"""TrimWorker and MergeWorker (Phase 5E) — the structural edits.

Everything in 5B–5D changes how a clip looks or sounds. These two change
what it *contains*, which is what separates editing from enhancing.

Two fixtures are used deliberately, because they differ in every way that
matters to merge: sample.mp4 is 1280x720, 30fps, 3s, **no audio**;
sample_with_audio.mp4 is 320x240, 15fps, 4s, **with audio**.
"""

import uuid
from pathlib import Path

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.base import PermanentError, StageMessage
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    primary_video,
    probe,
    run_ffmpeg,
)
from backend.workers.merge_worker import MergeWorker
from backend.workers.remove_segment_worker import RemoveSegmentWorker
from backend.workers.trim_worker import TrimWorker

_FIXTURES = Path(__file__).parent / "fixtures"
_SILENT = _FIXTURES / "sample.mp4"              # 1280x720, 30fps, 3s, no audio
_WITH_AUDIO = _FIXTURES / "sample_with_audio.mp4"  # 320x240, 15fps, 4s, audio


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    return disk


def _message(uris: list[str], params: dict, stage: int = 0) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["trim", "trim"],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": uris}}},
    )


async def _info(uri: str) -> dict:
    with materialize_to_tempfile(uri) as path:
        data = await probe(path)
    info = {"duration": float(data.get("format", {}).get("duration") or 0.0)}
    for stream in data["streams"]:
        if stream["codec_type"] == "video":
            info["size"] = (stream["width"], stream["height"])
            info["video_duration"] = float(stream.get("duration") or 0.0)
        else:
            info["audio_duration"] = float(stream.get("duration") or 0.0)
    return info


async def _sample_color(uri: str, at_seconds: float) -> tuple[int, int, int]:
    """The average RGB of a single frame, downscaled to one pixel so a
    (possibly compression-noisy) solid-colour test frame still reads back
    as unambiguously red/green/blue rather than needing an image library
    to do proper pixel analysis."""
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


# --- trim ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [({"start": 1}, 3.0), ({"end": 2}, 2.0), ({"start": 1, "end": 3}, 2.0)],
)
async def test_trim_produces_the_requested_range(
    ffmpeg_available, ffprobe_available, storage, params: dict, expected: float
) -> None:
    """start alone runs to the end, end alone runs from the beginning, and
    both keep only what lies between — none of which needs the input's
    duration to be known up front."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    payload = await TrimWorker().process(_message([source], params), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(expected, abs=0.15)


@pytest.mark.asyncio
async def test_trim_keeps_audio_and_video_in_sync(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    payload = await TrimWorker().process(_message([source], {"start": 1, "end": 3}), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert abs(info["video_duration"] - info["audio_duration"]) < 0.15


@pytest.mark.asyncio
async def test_trim_works_on_a_video_with_no_audio_track(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The atrim filter is applied unconditionally; ffmpeg ignores it when
    there is no audio stream, which is why this needs no probe."""
    source = storage.put(_SILENT.read_bytes(), suggested_name="silent.mp4")

    payload = await TrimWorker().process(_message([source], {"end": 2}), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(2.0, abs=0.15)


@pytest.mark.asyncio
async def test_trim_twice_applies_to_the_running_result(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """"Drop the intro, then the boring middle" — the workflow that made
    the old duplicate-stage-name rejection wrong (Phase 5A, Step 5)."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    first = await TrimWorker().process(_message([source], {"start": 1}), None)  # 4s -> 3s
    second = await TrimWorker().process(
        _message([source], {"end": 1.5}, stage=1), first
    )

    info = await _info(primary_video(previous_assets(second)).uri)
    assert info["video_duration"] == pytest.approx(1.5, abs=0.15)


@pytest.mark.asyncio
async def test_trim_forwards_unrelated_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    first = await TrimWorker().process(_message([source], {"end": 3}), None)
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")

    payload = await TrimWorker().process(
        _message([source], {"end": 2}, stage=1),
        {"assets": [*first["assets"], srt.to_dict()]},
    )

    assert srt in previous_assets(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},                              # neither bound
        {"start": -1},                   # negative
        {"start": 3, "end": 1},          # inverted
        {"start": 1, "end": 1},          # zero-length
        {"start": "1"},                  # not a number
        {"start": True},                 # bool is an int subclass
        {"from": 1},                     # unknown param
    ],
)
async def test_trim_rejects_bad_windows_permanently(storage, params: dict) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await TrimWorker().process(_message([source], params), None)

    assert isinstance(exc_info.value, PermanentError)


# --- merge -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_joins_clips_end_to_end(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    payload = await MergeWorker().process(_message([source, source], {}), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(8.0, abs=0.2), "4s + 4s"


@pytest.mark.asyncio
async def test_merge_uses_the_first_clips_frame_size(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """concat refuses mismatched dimensions outright, so clips are fitted
    onto one canvas — the first one's, so the result looks like what the
    user listed first rather than whichever clip happened to be biggest."""
    big = storage.put(_SILENT.read_bytes(), suggested_name="big.mp4")        # 1280x720
    small = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="small.mp4")  # 320x240

    payload = await MergeWorker().process(_message([big, small], {}), None)

    assert (await _info(primary_video(previous_assets(payload)).uri))["size"] == (1280, 720)


@pytest.mark.asyncio
async def test_merge_normalises_differing_frame_rates(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """concat joins streams without reconciling frame rate. Merging 30fps
    (3s) with 15fps (4s) lost ~70ms of the second clip until each was
    conformed to a single rate — the total must be exactly 7s."""
    thirty = storage.put(_SILENT.read_bytes(), suggested_name="30fps.mp4")
    fifteen = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="15fps.mp4")

    payload = await MergeWorker().process(_message([thirty, fifteen], {}), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(7.0, abs=0.15)


@pytest.mark.asyncio
async def test_merge_synthesises_silence_for_clips_without_audio(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """A stream-count mismatch would otherwise break concat. The silence
    must match its own clip's length, or the joined output is truncated."""
    silent = storage.put(_SILENT.read_bytes(), suggested_name="silent.mp4")   # 3s, no audio
    audible = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="loud.mp4")  # 4s, audio

    payload = await MergeWorker().process(_message([silent, audible], {}), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert "audio_duration" in info, "output should carry an audio track"
    assert info["audio_duration"] == pytest.approx(7.0, abs=0.2)


@pytest.mark.asyncio
async def test_merge_of_silent_clips_stays_silent(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """No audio anywhere means no invented audio track."""
    silent = storage.put(_SILENT.read_bytes(), suggested_name="silent.mp4")

    payload = await MergeWorker().process(_message([silent, silent], {}), None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert "audio_duration" not in info
    assert info["video_duration"] == pytest.approx(6.0, abs=0.2)


@pytest.mark.asyncio
async def test_merge_refuses_to_run_after_the_first_stage(storage) -> None:
    """The important guard. Later stages only ever see the original uploads
    in video_uris, so a mid-chain merge would rejoin the *unedited* sources
    and silently throw away every edit before it — worse than failing."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(InvalidMediaParamsError, match="first stage") as exc_info:
        await MergeWorker().process(
            _message([source, source], {}, stage=1),
            {"assets": [Asset(kind=AssetKind.VIDEO, uri=source).to_dict()]},
        )

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_merge_needs_at_least_two_videos(storage) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(InvalidMediaParamsError, match="at least two"):
        await MergeWorker().process(_message([source], {}), None)


@pytest.mark.asyncio
async def test_merge_then_trim_chains(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The workflow shape merge exists for: join first, then edit the
    joined result — not the other way round."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    merged = await MergeWorker().process(_message([source, source], {}), None)

    trimmed = await TrimWorker().process(
        _message([source], {"end": 5}, stage=1), merged
    )

    info = await _info(primary_video(previous_assets(trimmed)).uri)
    assert info["video_duration"] == pytest.approx(5.0, abs=0.2), (
        "must trim the 8s merged result, not the 4s original"
    )


# --- remove_segment ---------------------------------------------------------
#
# trim's complement: keeps everything *except* a range, rather than only
# what's inside it. Built after a live mix-up where "trim from 0:40 to
# 1:10" was read as "keep that range" when "remove that range" was meant
# — see both capabilities' descriptions in capability_registry.py.


@pytest.fixture
async def rgb_thirds_clip(ffmpeg_available) -> bytes:
    """A 3s clip: red for second 0-1, green for 1-2, blue for 2-3 —
    removing the middle second has an unambiguous, checkable result: the
    remainder must be red directly followed by blue, with no green frame
    anywhere and no gap between them."""
    with output_tempfile(".mp4") as destination:
        await run_ffmpeg(
            [
                "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1:r=10",
                "-f", "lavfi", "-i", "color=c=green:s=64x64:d=1:r=10",
                "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1:r=10",
                "-filter_complex", "concat=n=3:v=1:a=0",
                str(destination),
            ]
        )
        return destination.read_bytes()


@pytest.mark.asyncio
async def test_remove_segment_cuts_out_the_middle_and_rejoins(
    ffmpeg_available, ffprobe_available, storage, rgb_thirds_clip
) -> None:
    source = storage.put(rgb_thirds_clip, suggested_name="thirds.mp4")

    payload = await RemoveSegmentWorker().process(
        _message([source], {"start": 1, "end": 2}), None
    )

    uri = primary_video(previous_assets(payload)).uri
    info = await _info(uri)
    assert info["video_duration"] == pytest.approx(2.0, abs=0.2), "3s minus the 1s removed"

    start_color = await _sample_color(uri, 0.1)
    end_color = await _sample_color(uri, 1.9)
    assert start_color[0] > 150 and start_color[1] < 80, f"expected red, got {start_color}"
    assert end_color[2] > 150 and end_color[1] < 80, f"expected blue, got {end_color}"


@pytest.mark.asyncio
async def test_remove_segment_keeps_audio_and_video_in_sync(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")  # 4s, audio

    payload = await RemoveSegmentWorker().process(
        _message([source], {"start": 1, "end": 2}), None
    )

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(3.0, abs=0.2)
    assert abs(info["video_duration"] - info["audio_duration"]) < 0.2


@pytest.mark.asyncio
async def test_remove_segment_works_on_a_video_with_no_audio_track(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="silent.mp4")  # 3s, no audio

    payload = await RemoveSegmentWorker().process(
        _message([source], {"start": 0.5, "end": 1.5}), None
    )

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(2.0, abs=0.2)
    assert "audio_duration" not in info, "no audio in, no audio out"


@pytest.mark.asyncio
async def test_remove_segment_touching_the_start_degenerates_to_a_single_trim(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """No 'before' piece survives when start is ~0 -- this must fall back
    to a plain trim rather than feeding concat a near-empty first input."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")  # 4s

    payload = await RemoveSegmentWorker().process(
        _message([source], {"start": 0, "end": 1}), None
    )

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(3.0, abs=0.2)


@pytest.mark.asyncio
async def test_remove_segment_touching_the_end_degenerates_to_a_single_trim(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Symmetric case: no 'after' piece survives when end reaches (or
    exceeds) the input's real duration."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")  # 4s

    payload = await RemoveSegmentWorker().process(
        _message([source], {"start": 3, "end": 100}), None
    )

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(3.0, abs=0.2)


@pytest.mark.asyncio
async def test_remove_segment_covering_the_entire_video_is_rejected(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")  # 4s

    with pytest.raises(InvalidMediaParamsError, match="entire video"):
        await RemoveSegmentWorker().process(
            _message([source], {"start": 0, "end": 100}), None
        )


@pytest.mark.asyncio
async def test_remove_segment_forwards_unrelated_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    first = await RemoveSegmentWorker().process(
        _message([source], {"start": 1, "end": 2}), None
    )
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")

    payload = await TrimWorker().process(
        _message([source], {"end": 2}, stage=1),
        {"assets": [*first["assets"], srt.to_dict()]},
    )

    assert srt in previous_assets(payload)


@pytest.mark.asyncio
async def test_trim_then_remove_segment_chains(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Applies to the running result, same as repeated trim (Phase 5A,
    Step 5) — not to the original upload."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")  # 4s
    trimmed = await TrimWorker().process(_message([source], {"end": 4}), None)  # still 4s

    payload = await RemoveSegmentWorker().process(
        _message([source], {"start": 1, "end": 3}, stage=1), trimmed
    )

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["video_duration"] == pytest.approx(2.0, abs=0.2), "4s minus the 2s removed"


@pytest.mark.asyncio
async def test_remove_segment_honours_preview_mode(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Driving run_ffmpeg directly (needed for the filter_complex) means
    this capability can't fall through media.process_video's own preview
    handling -- it has to apply the scale filter itself, or a
    preview-proposal would silently render at full resolution."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")  # 320x240
    message = _message([source], {"start": 1, "end": 2})
    message.payload["_preview"] = True

    payload = await RemoveSegmentWorker().process(message, None)

    info = await _info(primary_video(previous_assets(payload)).uri)
    assert info["size"][1] <= 240, "already under the 480 preview cap -- must not upscale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"start": 1},                    # end missing
        {"end": 2},                      # start missing
        {},                              # both missing
        {"start": -1, "end": 2},         # negative
        {"start": 3, "end": 1},          # inverted
        {"start": 1, "end": 1},          # zero-length
        {"start": "1", "end": 2},        # not a number
        {"start": True, "end": 2},       # bool is an int subclass
        {"start": 1, "end": 2, "from": 1},  # unknown param
    ],
)
async def test_remove_segment_rejects_bad_windows_permanently(storage, params: dict) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await RemoveSegmentWorker().process(_message([source], params), None)

    assert isinstance(exc_info.value, PermanentError)
