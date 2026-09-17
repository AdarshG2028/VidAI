"""RenderWorker (Phase 5G) — the final deliverable."""

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
    previous_assets,
    primary_video,
    probe,
    video_stream,
)
from backend.workers.render_worker import (
    RenderWorker,
    _parse_bitrate,
    _parse_format,
    _parse_resolution,
)

_SAMPLE = Path(__file__).parent / "fixtures" / "sample_with_audio.mp4"  # 320x240


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    return disk


def _message(source_uri: str, params: dict, preview: bool = False) -> StageMessage:
    payload = {"stage_params": {"0": {"params": params, "video_uris": [source_uri]}}}
    if preview:
        payload["_preview"] = True
    return StageMessage(job_id=uuid.uuid4(), stage=0, workflow=["render"], payload=payload)


async def _codecs_and_size(uri: str) -> tuple[set[str], tuple[int, int]]:
    with materialize_to_tempfile(uri) as path:
        data = await probe(path)
    stream = video_stream(data)
    codecs = {s.get("codec_name") for s in data["streams"]}
    return codecs, (stream["width"], stream["height"])


async def _render(storage: LocalDiskStorage, params: dict, preview: bool = False) -> str:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")
    payload = await RenderWorker().process(_message(source, params, preview), None)
    return primary_video(previous_assets(payload)).uri


# --- parameter parsing (pure) ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [("1080p", 1080), ("720p", 720), ("1920x1080", 1080), (None, None)]
)
def test_resolution_accepts_shorthand_and_explicit(raw, expected) -> None:
    assert _parse_resolution(raw) == expected


@pytest.mark.parametrize("raw", ["4k", "huge", "1920x", "1920x0", 1080, True, "x1080"])
def test_resolution_rejects_nonsense(raw) -> None:
    with pytest.raises(InvalidMediaParamsError):
        _parse_resolution(raw)


@pytest.mark.parametrize(("raw", "expected"), [("5M", "5m"), ("2500k", "2500k"), (2500, "2500k")])
def test_bitrate_accepts_common_spellings(raw, expected) -> None:
    """A bare number means kbps, which is how bitrate is quoted in UIs."""
    assert _parse_bitrate(raw) == expected


@pytest.mark.parametrize("raw", ["fast", "5GB", True, "M5"])
def test_bitrate_rejects_nonsense(raw) -> None:
    with pytest.raises(InvalidMediaParamsError):
        _parse_bitrate(raw)


@pytest.mark.parametrize("raw", ["mp4", "MP4", ".mp4", "webm", "mov", "gif"])
def test_format_is_case_and_dot_insensitive(raw) -> None:
    assert _parse_format(raw) in {"mp4", "webm", "mov", "gif"}


@pytest.mark.parametrize("raw", ["avi", "mkv", "", None, 4])
def test_format_rejects_unsupported_containers(raw) -> None:
    with pytest.raises(InvalidMediaParamsError):
        _parse_format(raw)


# --- real renders ----------------------------------------------------------


@pytest.mark.asyncio
async def test_renders_mp4_by_default(ffmpeg_available, ffprobe_available, storage) -> None:
    codecs, _ = await _codecs_and_size(await _render(storage, {}))

    assert "h264" in codecs


@pytest.mark.asyncio
async def test_renders_webm_with_vp9_and_opus(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Format conversion has to change the *audio* codec too — copying aac
    into a webm container would produce a file nothing plays."""
    codecs, _ = await _codecs_and_size(await _render(storage, {"format": "webm"}))

    assert "vp9" in codecs
    assert "opus" in codecs


@pytest.mark.asyncio
async def test_renders_a_gif_with_no_audio(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    codecs, _ = await _codecs_and_size(await _render(storage, {"format": "gif"}))

    assert "gif" in codecs
    assert not {"aac", "opus", "mp3"} & codecs, "a gif must not carry an audio stream"


@pytest.mark.asyncio
async def test_resolution_scales_height_and_derives_width(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Width follows the aspect ratio rather than being forced — forcing
    both is what `resize` is for, and doing it silently at the last step
    would stretch the picture."""
    _, (width, height) = await _codecs_and_size(await _render(storage, {"resolution": "720p"}))

    assert height == 720
    assert width == pytest.approx(960, abs=2), "320x240 is 4:3, so 720 high is 960 wide"


@pytest.mark.asyncio
async def test_no_resolution_keeps_the_source_size(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    _, size = await _codecs_and_size(await _render(storage, {}))

    assert size == (320, 240)


@pytest.mark.asyncio
async def test_preview_caps_a_large_requested_resolution(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """A preview of a workflow ending in render must stay cheap, so the
    requested 1080p is capped rather than honoured."""
    _, (_, height) = await _codecs_and_size(
        await _render(storage, {"resolution": "1080p"}, preview=True)
    )

    assert height <= 480


@pytest.mark.asyncio
async def test_bitrate_is_applied(ffmpeg_available, ffprobe_available, storage) -> None:
    """Two renders differing only in bitrate must differ in size, or the
    parameter is being silently ignored."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")

    low = await RenderWorker().process(_message(source, {"bitrate": "100k"}), None)
    high = await RenderWorker().process(_message(source, {"bitrate": "3000k"}), None)

    low_uri = primary_video(previous_assets(low)).uri
    high_uri = primary_video(previous_assets(high)).uri
    assert len(storage.get(high_uri)) > len(storage.get(low_uri)) * 1.5


@pytest.mark.asyncio
async def test_render_forwards_other_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """A render at the end of a workflow must not drop the .srt a user
    still wants to download alongside the video."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")

    payload = await RenderWorker().process(
        _message(source, {}),
        {"assets": [Asset(kind=AssetKind.VIDEO, uri=source).to_dict(), srt.to_dict()]},
    )

    assert srt in previous_assets(payload)


@pytest.mark.asyncio
async def test_render_is_not_special_cased_as_a_final_stage(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """`render -> trim` — export a master, then cut a teaser from it. The
    engine treats the last stage generically, so nothing about render may
    assume it runs last."""
    from backend.workers.trim_worker import TrimWorker

    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")
    rendered = await RenderWorker().process(_message(source, {"resolution": "480p"}), None)

    trimmed = await TrimWorker().process(
        StageMessage(
            job_id=uuid.uuid4(),
            stage=1,
            workflow=["render", "trim"],
            payload={"stage_params": {"1": {"params": {"end": 2}, "video_uris": [source]}}},
        ),
        rendered,
    )

    with materialize_to_tempfile(primary_video(previous_assets(trimmed)).uri) as path:
        data = await probe(path)
    assert float(data["format"]["duration"]) == pytest.approx(2.0, abs=0.2)
    assert video_stream(data)["height"] == 480, "should trim the rendered 480p master"


@pytest.mark.asyncio
async def test_render_rejects_unknown_params(storage) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await RenderWorker().process(_message(source, {"codec": "h265"}), None)

    assert isinstance(exc_info.value, PermanentError)
