"""find_content -> remove_matches / keep_matches, end to end.

The acceptance test verifies the cut **by content, not duration**: a
12-second clip with red bands at known seconds, cut, then sampled pixel by
pixel. Duration alone would pass for a polarity inversion (9s and 3s are
both "a shorter video") or an off-by-one-interval mapping; sampling the
colour cannot.

No network: every test injects a FakeVisionClient, so the real Groq client
is never constructed. Frame extraction and the ffmpeg cut are real.
"""

import json
import uuid

import pytest

from backend.core.config import get_settings
from backend.services.vision_client import (
    FrameMatch,
    UnusableFramesError,
    VisionClient,
    VisionError,
)
from backend.storage.local import LocalDiskStorage
from backend.workers.base import StageMessage
from backend.workers.content_search_worker import ContentSearchWorker, _sample_interval
from backend.workers.match_edit_workers import (
    KeepMatchesWorker,
    RemoveMatchesWorker,
    complement,
)
from backend.workers.media import (
    AssetKind,
    InvalidMediaParamsError,
    MediaProcessingError,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    primary_video,
    probe,
    run_ffmpeg,
)

# Red bands in the fixture. Everything else is blue.
RED_BANDS = [(0, 1), (4, 5), (8, 9)]
CLIP_SECONDS = 12


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    monkeypatch.setattr("backend.workers.match_edit_workers.get_storage", lambda: disk)
    return disk


def _production_interval() -> float:
    """The interval find_content will really use on this fixture.

    Derived from the production function rather than hardcoded, so these
    tests exercise the shipped configuration instead of a monkeypatched
    one. An earlier version of this file patched the interval to 0.5s to
    make the acceptance test pass, which is exactly how the sampling bug it
    was papering over survived: at a flat 2s interval the three bands below
    coalesced into one 9s range and remove_matches cut 6 seconds the
    subject was never in.
    """
    return _sample_interval(float(CLIP_SECONDS), get_settings())


def test_production_sampling_can_resolve_the_fixtures_bands() -> None:
    """Guard on the two tests below, which would otherwise degrade quietly.

    They assert that three 1-second appearances are found separately. That
    is only possible if the shipped settings sample at least twice per
    band; if a future default coarsens past that, this fails with the
    reason rather than leaving the acceptance tests to fail on opaque
    duration arithmetic.
    """
    interval = _production_interval()

    assert interval <= 0.5, (
        f"production samples every {interval}s, too coarse for the 1s bands "
        "these tests are built on -- check vision_min_frames"
    )


async def _make_clip(bands: list[tuple[int, int]], *, with_audio: bool = False) -> bytes:
    """12s of 1-second solid-colour bands -- red inside `bands`, blue
    outside. Same lavfi-synthesis idiom as test_trim_merge_workers'
    rgb_thirds_clip: generated rather than checked in, so the exact seconds
    the assertions depend on are visible right here."""
    inputs: list[str] = []
    for second in range(CLIP_SECONDS):
        colour = "red" if any(s <= second < e for s, e in bands) else "blue"
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s=64x64:d=1:r=10"]
    with output_tempfile(".mp4") as video_only:
        await run_ffmpeg(
            [*inputs, "-filter_complex", f"concat=n={CLIP_SECONDS}:v=1:a=0", str(video_only)]
        )
        if not with_audio:
            return video_only.read_bytes()
        # Added in a second pass rather than interleaved into the concat --
        # simpler, and the audio only needs to exist for the sync check.
        with output_tempfile(".mp4") as with_sound:
            await run_ffmpeg(
                [
                    "-i", str(video_only),
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={CLIP_SECONDS}",
                    "-c:v", "copy", "-shortest", str(with_sound),
                ]
            )
            return with_sound.read_bytes()


@pytest.fixture
async def banded_clip(ffmpeg_available) -> bytes:
    return await _make_clip(RED_BANDS)


class FakeVisionClient(VisionClient):
    """Reports 'visible' for frames whose real timestamp falls in a red
    band -- standing in for what the model would say, without a network
    call. Records every batch so batching itself can be asserted."""

    def __init__(self, frame_times: dict[str, float] | None = None, *, raises=None) -> None:
        self._frame_times = frame_times or {}
        self._raises = raises
        self.batches: list[list[str]] = []

    async def inspect_frames(self, frames, *, query):
        self.batches.append([fid for fid, _ in frames])
        if self._raises is not None:
            raise self._raises
        verdicts = []
        for frame_id, _ in frames:
            t = self._frame_times.get(frame_id)
            visible = t is not None and any(s <= t < e for s, e in RED_BANDS)
            verdicts.append(FrameMatch(frame_id=frame_id, visible=visible, confidence=1.0))
        return verdicts


class TimeAwareFakeClient(VisionClient):
    """Resolves frame ids to times itself by asking the worker's own
    sampling maths, so the fixture's red bands drive the verdicts without
    the test needing to know the sampling interval up front."""

    def __init__(self, interval: float, bands: list[tuple[int, int]] | None = None) -> None:
        self._interval = interval
        # Which seconds are red in the clip THIS fake is being shown. Kept
        # a parameter rather than reading RED_BANDS directly: a fake whose
        # verdicts don't describe the clip under test reports matches that
        # aren't there, and the assertions downstream then measure nothing.
        self._bands = RED_BANDS if bands is None else bands
        self.batches: list[list[str]] = []

    async def inspect_frames(self, frames, *, query):
        self.batches.append([fid for fid, _ in frames])
        verdicts = []
        for frame_id, _ in frames:
            index = int(frame_id[1:])
            t = index * self._interval
            visible = any(s <= t < e for s, e in self._bands)
            verdicts.append(FrameMatch(frame_id=frame_id, visible=visible, confidence=1.0))
        return verdicts


def _message(uris: list[str], params: dict, stage: int = 0) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["find_content", "remove_matches"],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": uris}}},
    )


async def _every_frame_color(uri: str) -> list[tuple[int, int, int]]:
    """RGB of EVERY frame, from one ffmpeg pass.

    Each frame is scaled to a single pixel and written as raw rgb24, so the
    whole output is three bytes per frame and the test can check all of it
    rather than a grid of sample points.

    That distinction is not academic. This test previously sampled every
    0.5s, and at a coarser sampling interval the cut leaves 0.25s slivers
    of the subject at each segment edge -- which the grid stepped straight
    over, so "no red survived" passed on an output that still contained
    red. Checking every frame is what makes the assertion mean what it
    says.
    """
    with materialize_to_tempfile(uri) as path, output_tempfile(".rgb") as destination:
        await run_ffmpeg(
            [
                "-i", str(path),
                "-vf", "scale=1:1",
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                str(destination),
            ]
        )
        data = destination.read_bytes()
    return [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data) - 2, 3)]


async def _sample_color(uri: str, at_seconds: float) -> tuple[int, int, int]:
    """Average RGB of one frame, downscaled to a single pixel -- the same
    helper test_trim_merge_workers uses to verify a cut by content."""
    with materialize_to_tempfile(uri) as path, output_tempfile(".rgb") as destination:
        await run_ffmpeg(
            [
                "-ss", str(at_seconds), "-i", str(path),
                "-frames:v", "1", "-vf", "scale=1:1",
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                str(destination),
            ]
        )
        data = destination.read_bytes()
        return data[0], data[1], data[2]


def _is_red(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return r > 120 and b < 90


async def _duration(uri: str) -> float:
    with materialize_to_tempfile(uri) as path:
        data = await probe(path)
    return float(data["format"]["duration"])


# --- parameter handling (no ffmpeg, no client) ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{}, {"query": ""}, {"query": "   "}, {"query": 42}, {"query": "x" * 400}, {"q": "red shirt"}],
)
async def test_bad_queries_are_rejected_permanently(storage, params) -> None:
    """The validator has no required-params concept -- it only type-checks
    params that were supplied -- so an omitted query reaches the worker and
    must be caught here."""
    client = FakeVisionClient()
    with pytest.raises(InvalidMediaParamsError):
        await ContentSearchWorker(client).process(_message(["local://x.mp4"], params), None)

    assert client.batches == [], "no frames should be sent for an invalid query"


# --- the search itself ---------------------------------------------------


@pytest.mark.asyncio
async def test_find_content_emits_ranges_over_the_red_bands(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    uri = storage.put(banded_clip, suggested_name="banded.mp4")
    client = TimeAwareFakeClient(_production_interval())

    payload = await ContentSearchWorker(client).process(
        _message([uri], {"query": "a red frame"}), None
    )

    matches_asset = next(
        a for a in previous_assets(payload) if a.kind == AssetKind.CONTENT_MATCHES
    )
    data = json.loads(storage.get(matches_asset.uri))

    assert data["query"] == "a red frame"
    assert data["matches"], "expected the red bands to be found"
    # Every reported range must overlap a real red band.
    for match in data["matches"]:
        assert any(
            match["start"] < end and match["end"] > start for start, end in RED_BANDS
        ), f"range {match} overlaps no red band"


@pytest.mark.asyncio
async def test_find_content_forwards_the_video_when_it_is_stage_zero(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """The stage-0 trap. find_content is normally first, where nothing was
    carried -- without inserting the video asset, the next stage gets no
    video *and* no compiled video_uris and dies in resolve_input_uri."""
    uri = storage.put(banded_clip, suggested_name="banded.mp4")

    payload = await ContentSearchWorker(TimeAwareFakeClient(_production_interval())).process(
        _message([uri], {"query": "a red frame"}), None
    )

    assert primary_video(previous_assets(payload)) is not None


@pytest.mark.asyncio
async def test_frames_are_batched_within_the_provider_limit(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    uri = storage.put(banded_clip, suggested_name="banded.mp4")
    client = TimeAwareFakeClient(_production_interval())

    await ContentSearchWorker(client).process(_message([uri], {"query": "red"}), None)

    limit = get_settings().vision_frames_per_request
    assert client.batches, "expected at least one batch"
    for batch in client.batches:
        # Read from settings rather than hardcoded: the limit was 5 in the
        # model card and is 3 in the live API, and a literal here silently
        # stopped asserting anything when the setting was corrected.
        assert len(batch) <= limit, f"provider hard limit is {limit} images per request"
    ids = [fid for batch in client.batches for fid in batch]
    assert len(ids) == len(set(ids)), "every frame must be sent exactly once"


@pytest.mark.asyncio
async def test_a_vision_outage_fails_the_stage_and_is_retryable(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """Never interpreted as 'no matches in those frames' -- the coalescer
    bridges gaps, so a swallowed batch would be papered over and the stage
    would emit confidently wrong ranges."""
    uri = storage.put(banded_clip, suggested_name="banded.mp4")
    client = FakeVisionClient(raises=VisionError("provider down"))

    with pytest.raises(MediaProcessingError):
        await ContentSearchWorker(client).process(_message([uri], {"query": "red"}), None)


@pytest.mark.asyncio
async def test_rejected_frames_fail_permanently(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """Identical bytes fail identically, so this must not spend the retry
    budget."""
    uri = storage.put(banded_clip, suggested_name="banded.mp4")
    client = FakeVisionClient(raises=UnusableFramesError("rejected"))

    with pytest.raises(InvalidMediaParamsError):
        await ContentSearchWorker(client).process(_message([uri], {"query": "red"}), None)


@pytest.mark.asyncio
async def test_a_search_that_finds_nothing_is_not_an_error(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """find_content succeeded; it found nothing. The error belongs to the
    consumer, which knows the polarity -- same call detect_scenes makes for
    a single continuous shot."""
    uri = storage.put(banded_clip, suggested_name="banded.mp4")

    payload = await ContentSearchWorker(FakeVisionClient()).process(
        _message([uri], {"query": "a giraffe"}), None
    )

    asset = next(a for a in previous_assets(payload) if a.kind == AssetKind.CONTENT_MATCHES)
    assert json.loads(storage.get(asset.uri))["matches"] == []


# --- the acceptance test: verified by content ---------------------------


@pytest.mark.asyncio
async def test_remove_matches_cuts_out_every_red_band(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """The main acceptance test: no red may survive anywhere.

    Bands at 0-1, 4-5 and 8-9 coalesce (with 0.25s padding) to roughly
    0-1, 3.5-5 and 7.5-9, so the complement is three keep segments and the
    output is ~8s. Three bands touching the start of the clip give K=3
    rather than K=4 -- see the audio test below for the K=4 case.
    """
    uri = storage.put(banded_clip, suggested_name="banded.mp4")

    found = await ContentSearchWorker(TimeAwareFakeClient(_production_interval())).process(
        _message([uri], {"query": "a red frame"}), None
    )
    cut = await RemoveMatchesWorker().process(_message([uri], {}, stage=1), found)

    out = primary_video(previous_assets(cut)).uri
    duration = await _duration(out)
    assert duration == pytest.approx(8.0, abs=1.0), f"12s minus the red bands, got {duration}"

    # The assertion that matters -- a polarity inversion or a shifted
    # mapping cannot pass this, where a duration check alone would. Every
    # frame, not a grid: sampling on a 0.5s grid stepped over the 0.25s
    # slivers a coarser interval leaves behind.
    colors = await _every_frame_color(out)
    red_frames = [i for i, rgb in enumerate(colors) if _is_red(rgb)]

    assert not red_frames, (
        f"red survived in {len(red_frames)} of {len(colors)} frames, "
        f"first at frame {red_frames[0] if red_frames else None}"
    )


@pytest.mark.asyncio
async def test_keep_matches_keeps_only_the_red_bands(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """The inverse. Kept segments are ~0-1, 3.5-5 and 7.5-9 of the source,
    concatenated to ~4s.

    Sampled at the centre of each kept band rather than everywhere: the
    0.25s outward padding deliberately pulls in a little blue either side
    of each appearance, which is the documented bias toward keeping too
    much rather than clipping the subject's entry or exit.
    """
    uri = storage.put(banded_clip, suggested_name="banded.mp4")

    found = await ContentSearchWorker(TimeAwareFakeClient(_production_interval())).process(
        _message([uri], {"query": "a red frame"}), None
    )
    cut = await KeepMatchesWorker().process(_message([uri], {}, stage=1), found)

    out = primary_video(previous_assets(cut)).uri
    duration = await _duration(out)
    assert duration == pytest.approx(4.0, abs=1.0), f"~4s of red plus padding, got {duration}"

    # Centres of the three kept red stretches in output time.
    for at in (0.5, 2.0, 3.5):
        assert _is_red(await _sample_color(out, at)), f"expected red at {at}s of the output"


@pytest.mark.asyncio
async def test_four_segment_cut_keeps_audio_and_video_in_sync(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """K=4 with audio -- where both open questions live.

    Bands at 1-2, 4-5 and 8-9 touch neither end, so the complement is FOUR
    keep segments. That exercises the explicit split/asplit path at K>2,
    and the sync assertion is what would surface any drift between trim
    (frame boundaries) and atrim (sample boundaries) accumulating across
    segments.
    """
    bands = [(1, 2), (4, 5), (8, 9)]
    clip = await _make_clip(bands, with_audio=True)
    uri = storage.put(clip, suggested_name="banded_audio.mp4")

    found = await ContentSearchWorker(
        TimeAwareFakeClient(_production_interval(), bands)
    ).process(
        _message([uri], {"query": "a red frame"}), None
    )
    matches = json.loads(
        storage.get(
            next(a for a in previous_assets(found) if a.kind == AssetKind.CONTENT_MATCHES).uri
        )
    )
    assert len(matches["matches"]) == 3, f"expected three bands, got {matches['matches']}"

    # Asserted directly rather than inferred from the band count: the
    # point of this test is the K>2 split/asplit path, and a complement
    # that regressed to three segments would still satisfy every other
    # assertion here.
    keep = complement(
        [(m["start"], m["end"]) for m in matches["matches"]], duration=float(CLIP_SECONDS)
    )
    assert len(keep) == 4, f"this test exists to exercise K=4, got K={len(keep)}: {keep}"

    cut = await RemoveMatchesWorker().process(_message([uri], {}, stage=1), found)
    out = primary_video(previous_assets(cut)).uri

    with materialize_to_tempfile(out) as path:
        probe_data = await probe(path)
    streams = {s["codec_type"]: s for s in probe_data["streams"]}
    assert "audio" in streams, "audio must survive the cut"

    video_duration = float(streams["video"].get("duration") or 0.0)
    audio_duration = float(streams["audio"].get("duration") or 0.0)
    assert abs(video_duration - audio_duration) < 0.2, (
        f"a/v drift across four segments: video {video_duration}s, audio {audio_duration}s"
    )


# --- consumer guards -----------------------------------------------------


@pytest.mark.asyncio
async def test_remove_matches_without_find_content_names_the_missing_stage(storage) -> None:
    with pytest.raises(InvalidMediaParamsError, match="find_content"):
        await RemoveMatchesWorker().process(_message(["local://x.mp4"], {}, stage=1), None)


@pytest.mark.asyncio
async def test_consumers_take_no_parameters(storage) -> None:
    with pytest.raises(InvalidMediaParamsError, match="no parameters"):
        await RemoveMatchesWorker().process(
            _message(["local://x.mp4"], {"start": 1.0}, stage=1), None
        )


@pytest.mark.asyncio
async def test_remove_matches_on_an_empty_search_explains_itself(
    ffmpeg_available, ffprobe_available, storage, banded_clip
) -> None:
    """A search that found nothing is an error *here*, where the polarity is
    known, and the message must name the query so the room can rephrase."""
    uri = storage.put(banded_clip, suggested_name="banded.mp4")
    found = await ContentSearchWorker(FakeVisionClient()).process(
        _message([uri], {"query": "a giraffe"}), None
    )

    with pytest.raises(InvalidMediaParamsError, match="giraffe"):
        await RemoveMatchesWorker().process(_message([uri], {}, stage=1), found)
