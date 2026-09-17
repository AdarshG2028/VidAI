"""CropWorker (Phase 5B) — the first real editing capability.

Param parsing is pure and tested without ffmpeg; the encode tests are
gated on the ffmpeg/ffprobe fixtures and skip cleanly without them.

tests/fixtures/sample.mp4 is 1280x720 (16:9), video-only.
"""

import uuid

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.base import PermanentError, StageMessage
from backend.workers.crop_worker import CropWorker, _parse_aspect_ratio
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

from pathlib import Path

_SAMPLE = Path(__file__).parent / "fixtures" / "sample.mp4"


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    return disk


def _message(source_uri: str, aspect_ratio=None, stage: int = 0) -> StageMessage:
    params = {} if aspect_ratio is None else {"aspect_ratio": aspect_ratio}
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["crop"],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": [source_uri]}}},
    )


async def _dimensions(uri: str) -> tuple[int, int]:
    with materialize_to_tempfile(uri) as path:
        stream = video_stream(await probe(path))
    return stream["width"], stream["height"]


# --- aspect ratio parsing (pure) -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("9:16", 0.5625), ("16:9", 16 / 9), ("1:1", 1.0), ("4:5", 0.8), ("2.35:1", 2.35)],
)
def test_parses_valid_ratios(raw: str, expected: float) -> None:
    assert _parse_aspect_ratio(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    [
        None,          # param omitted entirely
        "",
        "banana",
        "16",          # no separator
        "16/9",        # wrong separator
        "9:0",         # division by zero
        "0:16",
        "-9:16",       # negative
        "9:16:1",      # partition() makes this "9" / "16:1" -> unparseable
        16 / 9,        # a float, not the "W:H" string the schema declares
        ["9", "16"],
    ],
)
def test_rejects_bad_ratios_permanently(raw) -> None:
    """All of these are the planner's mistake, not the environment's — they
    fail identically on redelivery, so they must DLQ on the first attempt
    rather than burn the retry budget."""
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        _parse_aspect_ratio(raw)

    assert isinstance(exc_info.value, PermanentError)


# --- real encodes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_crops_landscape_to_vertical(ffmpeg_available, ffprobe_available, storage) -> None:
    """The headline case: 16:9 -> 9:16 for Reels/Shorts."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")

    payload = await CropWorker().process(_message(source, "9:16"), None)

    produced = primary_video(previous_assets(payload))
    width, height = await _dimensions(produced.uri)
    assert width / height == pytest.approx(9 / 16, abs=0.01)
    assert height == 720, "should keep full height and narrow the width"


@pytest.mark.asyncio
async def test_output_dimensions_are_even(ffmpeg_available, ffprobe_available, storage) -> None:
    """h264/yuv420p cannot encode odd dimensions — 720 * 9/16 is 405, so
    without the floor(/2)*2 in the filter this exact case fails to encode."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")

    payload = await CropWorker().process(_message(source, "9:16"), None)

    width, height = await _dimensions(primary_video(previous_assets(payload)).uri)
    assert width % 2 == 0 and height % 2 == 0


@pytest.mark.asyncio
async def test_crops_to_square(ffmpeg_available, ffprobe_available, storage) -> None:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")

    payload = await CropWorker().process(_message(source, "1:1"), None)

    width, height = await _dimensions(primary_video(previous_assets(payload)).uri)
    assert width == height == 720


@pytest.mark.asyncio
async def test_already_matching_ratio_is_a_no_op_crop(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """min() keeps the rect inside the frame, so asking for the ratio the
    source already has must not enlarge or distort it."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")

    payload = await CropWorker().process(_message(source, "16:9"), None)

    assert await _dimensions(primary_video(previous_assets(payload)).uri) == (1280, 720)


@pytest.mark.asyncio
async def test_chains_from_a_previous_stage_and_forwards_other_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Crop must edit what the previous stage produced, and must not drop
    an unrelated asset (an srt) it was handed on the way through."""
    original = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    intermediate = (await CropWorker().process(_message(original, "1:1"), None))["assets"]
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")
    previous = {"assets": [*intermediate, srt.to_dict()]}

    payload = await CropWorker().process(_message(original, "9:16", stage=1), previous)

    assets = previous_assets(payload)
    width, height = await _dimensions(primary_video(assets).uri)
    # Cropping the 720x720 intermediate to 9:16 gives height 720 -> width
    # 404. Had it re-read the 1280x720 original, height would still be 720
    # but this asserts the srt survived, which only the chained path does.
    assert width / height == pytest.approx(9 / 16, abs=0.01)
    assert srt in assets, "an unrelated asset must survive the stage"


@pytest.mark.asyncio
async def test_missing_aspect_ratio_fails_before_touching_ffmpeg(storage) -> None:
    """No ffmpeg fixture: params are validated before any subprocess runs,
    so a bad proposal fails fast rather than after a download and encode."""
    source = storage.put(b"not even a video", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError):
        await CropWorker().process(_message(source, None), None)


@pytest.mark.asyncio
async def test_unusable_input_bytes_fail_permanently(ffmpeg_available, storage) -> None:
    """Corrupt input is the input's fault, not the environment's — it must
    reach the DLQ rather than be retried five times."""
    source = storage.put(b"definitely not a video", suggested_name="broken.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await CropWorker().process(_message(source, "9:16"), None)

    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_preview_mode_caps_resolution(ffmpeg_available, ffprobe_available, storage) -> None:
    """Preview is honoured without CropWorker knowing preview exists —
    process_video applies it, which is what stops each capability having to
    remember to."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    message = _message(source, "1:1")
    message.payload["_preview"] = True

    payload = await CropWorker().process(message, None)

    width, height = await _dimensions(primary_video(previous_assets(payload)).uri)
    assert height == 480 and width == 480
