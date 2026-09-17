"""ffmpeg/storage half of backend/workers/media.py.

Split from test_media_helpers.py because these need a real ffmpeg binary
and a real (temp-dir) storage backend, where those need nothing at all.
Tests that shell out are gated on the ffmpeg_available/ffprobe_available
fixtures and skip cleanly when the binaries are absent.

tests/fixtures/sample.mp4 is 1280x720, 3s, video-only — no audio stream.
Audio-filter coverage arrives with the audio capability (5D), which needs
a fixture that actually has sound.
"""

import uuid
from pathlib import Path

import pytest

import backend.workers.media as media
from backend.storage.local import LocalDiskStorage
from backend.workers.base import PermanentError, StageMessage
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    MediaProcessingError,
    is_preview,
    materialize_to_tempfile,
    output_tempfile,
    probe,
    process_video,
    put_asset,
    run_ffmpeg,
    video_stream,
)

_SAMPLE = Path(__file__).parent / "fixtures" / "sample.mp4"


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    """A real storage backend rooted in a temp dir, swapped in for the app's
    configured one so tests never touch ./data/storage."""
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    return disk


def _message(
    stage: int = 0,
    workflow: list[str] | None = None,
    video_uris: list[str] | None = None,
    preview: bool = False,
) -> StageMessage:
    payload = {
        "stage_params": {str(stage): {"params": {}, "video_uris": video_uris or []}}
    }
    if preview:
        payload["_preview"] = True
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=workflow if workflow is not None else ["crop"],
        payload=payload,
    )


async def _dimensions(uri: str, disk: LocalDiskStorage) -> tuple[int, int]:
    with materialize_to_tempfile(uri) as path:
        stream = video_stream(await probe(path))
    return stream["width"], stream["height"]


# --- materialize / output temp files ---------------------------------------


def test_materialize_writes_the_bytes_and_cleans_up(storage) -> None:
    uri = storage.put(b"some bytes", suggested_name="clip.mp4")

    with materialize_to_tempfile(uri) as path:
        assert path.read_bytes() == b"some bytes"
        assert path.suffix == ".mp4"
        held = path

    assert not held.exists(), "temp file must not outlive the context manager"


def test_materialize_cleans_up_even_when_the_body_raises(storage) -> None:
    uri = storage.put(b"some bytes", suggested_name="clip.mp4")
    held = None

    with pytest.raises(RuntimeError):
        with materialize_to_tempfile(uri) as path:
            held = path
            raise RuntimeError("boom")

    assert held is not None and not held.exists()


def test_materialize_raises_permanently_for_an_unknown_uri(storage) -> None:
    """A missing object will still be missing on the next attempt, so this
    must skip the retry budget rather than spend it."""
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        with materialize_to_tempfile("local://nope.mp4"):
            pass

    assert isinstance(exc_info.value, PermanentError)


def test_output_tempfile_yields_an_unused_path_and_cleans_up() -> None:
    with output_tempfile(".mp4") as path:
        assert not path.exists(), "ffmpeg should create the output itself"
        assert path.suffix == ".mp4"
        path.write_bytes(b"x")
        parent = path.parent

    assert not parent.exists()


def test_put_asset_uploads_and_describes(storage, tmp_path) -> None:
    local = tmp_path / "out.mp4"
    local.write_bytes(b"encoded")

    asset = put_asset(local)

    assert asset.kind == AssetKind.VIDEO
    assert storage.get(asset.uri) == b"encoded"


# --- run_ffmpeg / probe ----------------------------------------------------


@pytest.mark.asyncio
async def test_run_ffmpeg_raises_retryably_when_the_binary_is_missing(monkeypatch) -> None:
    """ffmpeg absent is an environment problem, not an input problem — it
    must stay retryable, since a redeploy or a different worker instance
    could still succeed. Forced rather than requiring an uninstall."""

    async def _raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(
        "backend.workers.media.asyncio.create_subprocess_exec", _raise_not_found
    )

    with pytest.raises(MediaProcessingError) as exc_info:
        await run_ffmpeg(["-i", "whatever.mp4", "out.mp4"])

    assert not isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_run_ffmpeg_raises_permanently_on_a_nonzero_exit(ffmpeg_available) -> None:
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await run_ffmpeg(["-i", "definitely-not-a-file.mp4", "out.mp4"])

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_run_ffmpeg_returns_stderr_for_measurement(ffmpeg_available) -> None:
    """Capabilities and tests read ffmpeg's analysis filters out of stderr —
    that's what avoids adding a pixel/audio library to this project."""
    stderr = await run_ffmpeg(
        ["-i", str(_SAMPLE), "-vf", "signalstats", "-f", "null", "-"]
    )

    assert "frame=" in stderr or "Stream" in stderr


@pytest.mark.asyncio
async def test_probe_reads_real_metadata(ffprobe_available) -> None:
    data = await probe(_SAMPLE)

    stream = video_stream(data)
    assert stream["width"] == 1280
    assert stream["height"] == 720


@pytest.mark.asyncio
async def test_probe_raises_permanently_on_unusable_input(ffprobe_available, tmp_path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")

    with pytest.raises(InvalidMediaParamsError):
        await probe(junk)


def test_video_stream_raises_when_there_is_no_video() -> None:
    with pytest.raises(InvalidMediaParamsError, match="no video stream"):
        video_stream({"streams": [{"codec_type": "audio"}]})


# --- is_preview ------------------------------------------------------------


def test_is_preview_reflects_the_payload_flag() -> None:
    assert is_preview(_message(preview=True)) is True
    assert is_preview(_message()) is False


# --- process_video (the workhorse) -----------------------------------------


@pytest.mark.asyncio
async def test_process_video_applies_filters_and_stores_the_result(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source_uri = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    message = _message(video_uris=[source_uri])

    asset = await process_video(message, None, video_filters=["crop=640:720:0:0"])

    assert asset.kind == AssetKind.VIDEO
    assert asset.uri != source_uri, "must store a new object, not mutate the input"
    assert await _dimensions(asset.uri, storage) == (640, 720)


@pytest.mark.asyncio
async def test_process_video_reads_the_previous_stages_output(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The chaining property, end to end: stage 1 must edit what stage 0
    produced, not re-read the original upload."""
    original_uri = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    first = await process_video(
        _message(video_uris=[original_uri]), None, video_filters=["crop=640:720:0:0"]
    )

    second_stage = _message(stage=1, workflow=["crop", "scale"], video_uris=[original_uri])
    second = await process_video(
        second_stage,
        {"assets": [first.to_dict()]},
        video_filters=["scale=iw/2:-2"],
    )

    # A filter relative to its own input is what makes this discriminating:
    # halving the 640-wide intermediate gives width 320, halving the
    # 1280-wide original would give 640. An absolute crop would have
    # produced the same number either way and proved nothing. (-2 scales
    # height proportionally, hence 360 rather than the intermediate's 720.)
    assert await _dimensions(second.uri, storage) == (320, 360)
    assert second.uri not in {original_uri, first.uri}


@pytest.mark.asyncio
async def test_process_video_caps_resolution_in_preview_mode(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source_uri = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")

    full = await process_video(_message(video_uris=[source_uri]), None)
    preview = await process_video(_message(video_uris=[source_uri], preview=True), None)

    assert await _dimensions(full.uri, storage) == (1280, 720)
    assert await _dimensions(preview.uri, storage) == (854, 480)


@pytest.mark.asyncio
async def test_preview_never_upscales_a_smaller_source(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """min(ih, cap) rather than a flat scale — a 240p source must stay 240p
    instead of being blown up to the preview cap."""
    source_uri = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    small = await process_video(
        _message(video_uris=[source_uri]), None, video_filters=["scale=-2:240"]
    )

    preview = await process_video(
        _message(video_uris=[source_uri], preview=True), {"assets": [small.to_dict()]}
    )

    assert await _dimensions(preview.uri, storage) == await _dimensions(small.uri, storage)


@pytest.mark.asyncio
async def test_process_video_surfaces_bad_params_permanently(
    ffmpeg_available, storage
) -> None:
    """A crop rect larger than the source is a params problem: it fails the
    same way on every redelivery, so it belongs in the DLQ immediately."""
    source_uri = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    message = _message(video_uris=[source_uri])

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await process_video(message, None, video_filters=["crop=9999:9999:0:0"])

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_process_video_reports_a_missing_input_rather_than_running_ffmpeg(
    storage,
) -> None:
    message = _message(video_uris=[])

    with pytest.raises(InvalidMediaParamsError, match="no input video"):
        await process_video(message, None)


@pytest.mark.asyncio
async def test_full_quality_encodes_use_the_configured_preset(
    ffmpeg_available, storage, monkeypatch
) -> None:
    """libx264's own default is `medium`. Measured on a 140s 1080p clip,
    `veryfast` halved the encode *and* produced a smaller file, so leaving
    the default in place was costing time for nothing."""
    captured: list[list[str]] = []
    real = media.run_ffmpeg

    async def spy(args, **kwargs):
        captured.append(args)
        return await real(args, **kwargs)

    monkeypatch.setattr("backend.workers.media.run_ffmpeg", spy)
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="s.mp4")

    await process_video(_message(video_uris=[source]), None, video_filters=["scale=160:-2"])

    assert "-preset" in captured[0]
    assert captured[0][captured[0].index("-preset") + 1] == "veryfast"


@pytest.mark.asyncio
async def test_preview_still_overrides_with_ultrafast(
    ffmpeg_available, storage, monkeypatch
) -> None:
    """Preview optimises for turnaround, not size — it must not silently
    inherit the full-quality preset."""
    captured: list[list[str]] = []
    real = media.run_ffmpeg

    async def spy(args, **kwargs):
        captured.append(args)
        return await real(args, **kwargs)

    monkeypatch.setattr("backend.workers.media.run_ffmpeg", spy)
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="s.mp4")

    await process_video(_message(video_uris=[source], preview=True), None)

    assert captured[0][captured[0].index("-preset") + 1] == "ultrafast"


@pytest.mark.parametrize("megabytes", [1, 8, 32])
def test_materialising_holds_constant_memory_regardless_of_size(storage, megabytes) -> None:
    """Measured before this was streamed: a 200MB video meant a 200MB
    resident spike, per worker, per stage — enough to OOM a small
    instance running several workers, for bytes that were only ever going
    straight to disk."""
    import tracemalloc

    uri = storage.put(b"\0" * (megabytes * 1024 * 1024), suggested_name="big.mp4")

    tracemalloc.start()
    with materialize_to_tempfile(uri) as path:
        written = path.stat().st_size
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert written == megabytes * 1024 * 1024, "must still copy the file faithfully"
    assert peak < 4 * 1024 * 1024, (
        f"held {peak / 1024 / 1024:.1f}MB for a {megabytes}MB file — not streaming"
    )


def test_storing_an_output_holds_constant_memory(storage, tmp_path) -> None:
    """The same waste on the write side: put(path.read_bytes()) read a
    whole render into memory purely to hand it to storage."""
    import tracemalloc

    source = tmp_path / "render.mp4"
    source.write_bytes(b"\0" * (32 * 1024 * 1024))

    tracemalloc.start()
    asset = put_asset(source)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert storage.size(asset.uri) == 32 * 1024 * 1024
    assert peak < 4 * 1024 * 1024
