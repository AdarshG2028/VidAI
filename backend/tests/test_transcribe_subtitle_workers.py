"""transcribe + burn_subtitles (Phase 5F).

The provider call is faked; everything around it is real. Audio really is
extracted with ffmpeg, the SRT really is rendered, and it really is burned
into a video — so the parts that can break silently are all exercised,
without a paid API call or a network dependency in the test suite.
(conftest also strips GROQ_API_KEY, so a real call could not happen by
accident.)
"""

import uuid
from pathlib import Path

import pytest

from backend.services.transcription_client import (
    TranscriptionClient,
    TranscriptionError,
    TranscriptionResult,
    TranscriptSegment,
    UnusableAudioError,
)
from backend.storage.local import LocalDiskStorage
from backend.workers.base import PermanentError, StageMessage
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    MediaProcessingError,
    materialize_to_tempfile,
    previous_assets,
    primary_video,
    probe,
    video_stream,
)
from backend.workers.subtitle_burn_worker import SubtitleBurnWorker
from backend.workers.transcribe_worker import TranscribeWorker, build_srt

_FIXTURES = Path(__file__).parent / "fixtures"
_WITH_AUDIO = _FIXTURES / "sample_with_audio.mp4"
_SILENT = _FIXTURES / "sample.mp4"

_SEGMENTS = [
    TranscriptSegment(start=0.0, end=1.5, text="Hello and welcome"),
    TranscriptSegment(start=1.6, end=3.2, text="to the second line"),
]


class FakeTranscriber(TranscriptionClient):
    def __init__(self, *, segments=None, error: Exception | None = None) -> None:
        self._segments = _SEGMENTS if segments is None else segments
        self._error = error
        self.received_audio: bytes | None = None
        self.received_language: str | None = None
        self.calls = 0

    async def transcribe(self, audio, *, filename, language=None):
        self.calls += 1
        if self._error:
            raise self._error
        self.received_audio = audio
        self.received_language = language
        return TranscriptionResult(
            text=" ".join(s.text for s in self._segments),
            language="en",
            segments=list(self._segments),
        )


class FakeVideoAssetCache:
    """In-memory VideoAssetCache double -- no database in these tests."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Asset] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        self._put_error: Exception | None = None
        self._get_error: Exception | None = None

    def seed(self, video_id: str, kind: str, asset: Asset) -> None:
        self.store[(video_id, kind)] = asset

    def fail_puts_with(self, error: Exception) -> None:
        self._put_error = error

    def fail_gets_with(self, error: Exception) -> None:
        self._get_error = error

    async def get(self, video_id: str, kind: str) -> Asset | None:
        self.get_calls.append((video_id, kind))
        if self._get_error:
            raise self._get_error
        return self.store.get((video_id, kind))

    async def put(self, video_id: str, kind: str, asset: Asset, data=None) -> None:
        self.put_calls.append((video_id, kind))
        if self._put_error:
            raise self._put_error
        self.store[(video_id, kind)] = asset


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    monkeypatch.setattr("backend.storage.get_storage", lambda: disk)
    return disk


def _message(
    uris: list[str], params: dict, stage: int = 0, video_ids: list[str] | None = None
) -> StageMessage:
    entry = {"params": params, "video_uris": uris}
    if video_ids is not None:
        entry["video_ids"] = video_ids
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["transcribe", "burn_subtitles"],
        payload={"stage_params": {str(stage): entry}},
    )


# --- SRT rendering (pure) --------------------------------------------------


def test_srt_has_the_shape_players_require() -> None:
    """1-indexed cues, HH:MM:SS,mmm timestamps, blank line between blocks.
    ffmpeg's subtitles filter is strict about all three."""
    srt = build_srt(_SEGMENTS)

    assert srt == (
        "1\n00:00:00,000 --> 00:00:01,500\nHello and welcome\n"
        "\n"
        "2\n00:00:01,600 --> 00:00:03,200\nto the second line\n"
    )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00,000"),
        (1.5, "00:00:01,500"),
        (61.25, "00:01:01,250"),
        (3661.001, "01:01:01,001"),
        (0.9999, "00:00:01,000"),  # rounding must carry into the second
    ],
)
def test_timestamps_are_formatted_correctly(seconds: float, expected: str) -> None:
    from backend.workers.transcribe_worker import _timestamp

    assert _timestamp(seconds) == expected


# --- transcribe ------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_produces_transcript_and_srt_assets(
    ffmpeg_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    payload = await TranscribeWorker(FakeTranscriber()).process(
        _message([source], {}), None
    )

    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "transcript", "srt"}


@pytest.mark.asyncio
async def test_transcribe_passes_the_video_through_untouched(
    ffmpeg_available, storage
) -> None:
    """The stage never re-encodes: its video asset must be the *same
    stored object* it was given, so transcribe costs no quality wherever
    it sits in a workflow."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    payload = await TranscribeWorker(FakeTranscriber()).process(
        _message([source], {}), None
    )

    assert primary_video(previous_assets(payload)).uri == source


@pytest.mark.asyncio
async def test_transcribe_uploads_compressed_mono_audio_not_the_video(
    ffmpeg_available, storage
) -> None:
    """The provider caps upload size, which is why audio is extracted
    rather than the video sent. It must come out dramatically smaller."""
    video_bytes = _WITH_AUDIO.read_bytes()
    source = storage.put(video_bytes, suggested_name="clip.mp4")
    fake = FakeTranscriber()

    await TranscribeWorker(fake).process(_message([source], {}), None)

    assert fake.received_audio is not None
    assert len(fake.received_audio) < len(video_bytes) / 2


@pytest.mark.asyncio
async def test_language_hint_is_forwarded_when_given(ffmpeg_available, storage) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    fake = FakeTranscriber()

    await TranscribeWorker(fake).process(_message([source], {"language": "es"}), None)

    assert fake.received_language == "es"


@pytest.mark.asyncio
async def test_language_is_omitted_when_not_given(ffmpeg_available, storage) -> None:
    """Whisper detects language itself; forcing a wrong one degrades the
    transcript badly, so absence must stay absence rather than a default."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    fake = FakeTranscriber()

    await TranscribeWorker(fake).process(_message([source], {}), None)

    assert fake.received_language is None


@pytest.mark.asyncio
async def test_video_with_no_audio_track_fails_permanently(
    ffmpeg_available, storage
) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="silent.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await TranscribeWorker(FakeTranscriber()).process(_message([source], {}), None)

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_no_speech_found_fails_permanently(ffmpeg_available, storage) -> None:
    """An empty transcript is not a transient failure — the same audio
    yields the same nothing on every retry."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await TranscribeWorker(FakeTranscriber(segments=[])).process(
            _message([source], {}), None
        )

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_provider_outage_is_retryable(ffmpeg_available, storage) -> None:
    """A network failure is the environment's fault, so it must retry
    rather than DLQ — the opposite of the rejected-audio case below."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(MediaProcessingError) as exc_info:
        await TranscribeWorker(
            FakeTranscriber(error=TranscriptionError("connection reset"))
        ).process(_message([source], {}), None)

    assert not isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_rejected_audio_is_permanent(ffmpeg_available, storage) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await TranscribeWorker(
            FakeTranscriber(error=UnusableAudioError("413 too large"))
        ).process(_message([source], {}), None)

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_transcribe_rejects_bad_params(storage) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    for params in ({"language": ""}, {"language": 42}, {"lang": "en"}):
        with pytest.raises(InvalidMediaParamsError):
            await TranscribeWorker(FakeTranscriber()).process(
                _message([source], params), None
            )


# --- transcript caching (Phase 10 foundation) ------------------------------


@pytest.mark.asyncio
async def test_cache_hit_skips_transcription_and_still_forwards_video_asset(
    ffmpeg_available, storage
) -> None:
    """The discriminating case: a cache hit must still assemble the exact
    same result shape a real transcription would (video + transcript +
    srt). The early-return path shares _finish with the real path
    specifically so this cannot regress."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cached_transcript = Asset(kind=AssetKind.TRANSCRIPT, uri="local://cached-transcript.json")
    cached_srt = Asset(kind=AssetKind.SRT, uri="local://cached-captions.srt")
    cache.seed(video_id, AssetKind.TRANSCRIPT, cached_transcript)
    cache.seed(video_id, AssetKind.SRT, cached_srt)
    fake = FakeTranscriber()

    payload = await TranscribeWorker(fake, cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    assert fake.calls == 0, "a cache hit must never call the transcription provider"
    assets = previous_assets(payload)
    assert primary_video(assets).uri == source
    assert next(a for a in assets if a.kind == AssetKind.TRANSCRIPT).uri == cached_transcript.uri
    assert next(a for a in assets if a.kind == AssetKind.SRT).uri == cached_srt.uri


@pytest.mark.asyncio
async def test_cache_miss_writes_through_after_transcribing(
    ffmpeg_available, storage
) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    fake = FakeTranscriber()

    payload = await TranscribeWorker(fake, cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    assert fake.calls == 1
    assert cache.put_calls == [(video_id, AssetKind.TRANSCRIPT), (video_id, AssetKind.SRT)]
    produced = next(a for a in previous_assets(payload) if a.kind == AssetKind.TRANSCRIPT)
    assert cache.store[(video_id, AssetKind.TRANSCRIPT)].uri == produced.uri


@pytest.mark.asyncio
async def test_cache_partial_hit_falls_back_to_real_transcription(
    ffmpeg_available, storage
) -> None:
    """Both or neither: a transcript cached with no matching srt (an
    interrupted write) must not be served as a hit."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.seed(
        video_id, AssetKind.TRANSCRIPT, Asset(kind=AssetKind.TRANSCRIPT, uri="local://t.json")
    )
    fake = FakeTranscriber()

    payload = await TranscribeWorker(fake, cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    assert fake.calls == 1, "a partial hit must fall through to real transcription"
    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "transcript", "srt"}


@pytest.mark.asyncio
async def test_cache_is_bypassed_for_a_language_override(
    ffmpeg_available, storage
) -> None:
    """A forced language can produce a different transcript than the
    cached (auto-detected) one, so it must neither read nor write the
    shared cache entry."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.seed(
        video_id,
        AssetKind.TRANSCRIPT,
        Asset(kind=AssetKind.TRANSCRIPT, uri="local://should-not-be-used.json"),
    )
    cache.seed(
        video_id, AssetKind.SRT, Asset(kind=AssetKind.SRT, uri="local://should-not-be-used.srt")
    )
    fake = FakeTranscriber()

    await TranscribeWorker(fake, cache=cache).process(
        _message([source], {"language": "es"}, video_ids=[video_id]), None
    )

    assert fake.calls == 1
    assert cache.get_calls == []
    assert cache.put_calls == []


@pytest.mark.asyncio
async def test_cache_is_bypassed_once_a_prior_stage_produced_a_video(
    ffmpeg_available, storage
) -> None:
    """Caching only applies to the pristine upload's audio. If an earlier
    stage already re-encoded the video (chained in via previous_output),
    reusing the original video's transcript would be wrong -- e.g. a trim
    changes what audio actually plays."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.seed(
        video_id,
        AssetKind.TRANSCRIPT,
        Asset(kind=AssetKind.TRANSCRIPT, uri="local://should-not-be-used.json"),
    )
    cache.seed(
        video_id, AssetKind.SRT, Asset(kind=AssetKind.SRT, uri="local://should-not-be-used.srt")
    )
    fake = FakeTranscriber()
    previous_output = {"assets": [Asset(kind=AssetKind.VIDEO, uri=source).to_dict()]}

    await TranscribeWorker(fake, cache=cache).process(
        _message([source], {}, stage=1, video_ids=[video_id]), previous_output
    )

    assert fake.calls == 1
    assert cache.get_calls == []
    assert cache.put_calls == []


@pytest.mark.asyncio
async def test_no_cache_key_when_video_ids_were_never_compiled(
    ffmpeg_available, storage
) -> None:
    """A payload built without compile_workflow's video_ids (or any other
    legacy shape) must degrade to "no cache", not raise."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    cache = FakeVideoAssetCache()
    fake = FakeTranscriber()

    await TranscribeWorker(fake, cache=cache).process(_message([source], {}), None)

    assert fake.calls == 1
    assert cache.get_calls == []
    assert cache.put_calls == []


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_fail_the_stage(
    ffmpeg_available, storage
) -> None:
    """A caching side-effect must never dead-letter a job whose
    transcription itself succeeded."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.fail_puts_with(RuntimeError("db unreachable"))
    fake = FakeTranscriber()

    payload = await TranscribeWorker(fake, cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "transcript", "srt"}


@pytest.mark.asyncio
async def test_cache_read_failure_falls_back_to_real_transcription(
    ffmpeg_available, storage
) -> None:
    """A database blip on the read side must degrade to a cache miss, not
    fail a stage a real transcription would have completed successfully --
    the same guarantee the write path already has."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.fail_gets_with(RuntimeError("db unreachable"))
    fake = FakeTranscriber()

    payload = await TranscribeWorker(fake, cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    assert fake.calls == 1
    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "transcript", "srt"}


# --- burn_subtitles --------------------------------------------------------


async def _burn(storage: LocalDiskStorage, params: dict | None = None):
    """transcribe then burn, the way a real workflow runs them."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    transcribed = await TranscribeWorker(FakeTranscriber()).process(
        _message([source], {}), None
    )
    return await SubtitleBurnWorker().process(
        _message([source], params or {}, stage=1), transcribed
    )


@pytest.mark.asyncio
async def test_burn_produces_a_new_video(ffmpeg_available, ffprobe_available, storage) -> None:
    """Proves the whole Windows-path problem is solved: ffmpeg's subtitles
    filter parses its path with the filtergraph lexer, where backslashes
    escape and colons separate options, so an absolute Windows path is
    destroyed. Running from the file's own directory sidesteps it."""
    payload = await _burn(storage)

    burned = primary_video(previous_assets(payload))
    with materialize_to_tempfile(burned.uri) as path:
        stream = video_stream(await probe(path))
    assert (stream["width"], stream["height"]) == (320, 240)


@pytest.mark.asyncio
async def test_burn_keeps_the_srt_available_afterwards(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Burning captions in must not consume the subtitle file — a user
    still wants the .srt to upload alongside the video."""
    payload = await _burn(storage)

    kinds = {a.kind for a in previous_assets(payload)}
    assert {"srt", "transcript"} <= kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["default", "large", "small"])
async def test_burn_accepts_each_style(
    ffmpeg_available, ffprobe_available, storage, style: str
) -> None:
    payload = await _burn(storage, {"style": style})

    assert primary_video(previous_assets(payload)) is not None


@pytest.mark.asyncio
async def test_burn_without_a_subtitle_asset_fails_clearly(storage) -> None:
    """Reaching this worker with no srt means the job bypassed the planner
    (validate_proposal rejects it), so the message says what to add."""
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(InvalidMediaParamsError, match="transcribe") as exc_info:
        await SubtitleBurnWorker().process(
            _message([source], {}, stage=1),
            {"assets": [Asset(kind=AssetKind.VIDEO, uri=source).to_dict()]},
        )

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_burn_rejects_an_unknown_style(storage) -> None:
    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")

    with pytest.raises(InvalidMediaParamsError, match="style"):
        await SubtitleBurnWorker().process(
            _message([source], {"style": "neon"}, stage=1),
            {"assets": [Asset(kind=AssetKind.SRT, uri="local://x.srt").to_dict()]},
        )


@pytest.mark.asyncio
async def test_the_two_need_not_be_adjacent(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The property monotonic accumulation exists for: an unrelated stage
    between transcribe and burn_subtitles must not lose the srt."""
    from backend.workers.color_worker import ColorWorker

    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    transcribed = await TranscribeWorker(FakeTranscriber()).process(
        _message([source], {}), None
    )
    graded = await ColorWorker().process(
        _message([source], {"brightness": 0.1}, stage=1), transcribed
    )

    payload = await SubtitleBurnWorker().process(
        _message([source], {}, stage=2), graded
    )

    assert primary_video(previous_assets(payload)) is not None
    assert any(a.kind == AssetKind.SRT for a in previous_assets(payload))


@pytest.mark.asyncio
async def test_burn_actually_draws_pixels_not_just_re_encodes(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The failure this catches is a silent one: ffmpeg exits 0 and writes
    a perfectly valid video when a subtitles filter matches nothing, so
    every other assertion here would still pass with no captions drawn.

    Measured on the bottom third of the frame, where captions sit. The
    threshold is calibrated against a measured noise floor: a plain
    re-encode of the same clip moves this value by 0.026, while burning
    captions moves it by ~1.2 — a 47x margin, so 0.3 separates them
    without being brittle.
    """
    import re

    from backend.workers.media import run_ffmpeg

    async def bottom_third_luma(uri: str) -> float:
        with materialize_to_tempfile(uri) as path:
            stderr = await run_ffmpeg(
                [
                    "-i", str(path),
                    "-vf",
                    "crop=iw:ih/3:0:ih*2/3,signalstats,"
                    "metadata=print:key=lavfi.signalstats.YAVG",
                    "-f", "null", "-",
                ]
            )
        values = [float(m) for m in re.findall(r"signalstats\.YAVG=([\d.]+)", stderr)]
        assert values, "no luma samples parsed"
        return sum(values) / len(values)

    source = storage.put(_WITH_AUDIO.read_bytes(), suggested_name="clip.mp4")
    transcribed = await TranscribeWorker(FakeTranscriber()).process(
        _message([source], {}), None
    )
    burned = await SubtitleBurnWorker().process(
        _message([source], {"style": "large"}, stage=1), transcribed
    )

    before = await bottom_third_luma(source)
    after = await bottom_third_luma(primary_video(previous_assets(burned)).uri)

    assert abs(after - before) > 0.3, (
        f"captions do not appear to have been drawn (luma {before:.3f} -> {after:.3f})"
    )
