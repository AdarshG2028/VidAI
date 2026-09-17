"""resize / rotate / flip / pad — the rest of Phase 5B's spatial transforms.

Param validation is pure and needs no ffmpeg; the encode tests are gated
on the ffmpeg/ffprobe fixtures.

tests/fixtures/sample.mp4 is 1280x720 (16:9), video-only.
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
    previous_assets,
    primary_video,
    probe,
    video_stream,
)
from backend.workers.transform_workers import (
    FlipWorker,
    PadWorker,
    ResizeWorker,
    RotateWorker,
    _even,
)

_SAMPLE = Path(__file__).parent / "fixtures" / "sample.mp4"


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    return disk


def _message(source_uri: str, params: dict, stage: int = 0) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["transform"],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": [source_uri]}}},
    )


async def _dimensions(uri: str) -> tuple[int, int]:
    with materialize_to_tempfile(uri) as path:
        stream = video_stream(await probe(path))
    return stream["width"], stream["height"]


async def _apply(worker, storage: LocalDiskStorage, params: dict) -> tuple[int, int]:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    payload = await worker.process(_message(source, params), None)
    return await _dimensions(primary_video(previous_assets(payload)).uri)


# --- even-dimension helper -------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [(720, 720), (721, 720), (1, 2), (0, 2), (3, 2)])
def test_even_rounds_down_and_floors_at_two(value: int, expected: int) -> None:
    assert _even(value) == expected


# --- resize ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_to_both_dimensions(ffmpeg_available, ffprobe_available, storage) -> None:
    assert await _apply(ResizeWorker(), storage, {"width": 640, "height": 360}) == (640, 360)


@pytest.mark.asyncio
async def test_resize_by_width_alone_preserves_aspect(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Giving one dimension must scale proportionally, not squash — 640 on a
    16:9 source means 360, not the original 720."""
    assert await _apply(ResizeWorker(), storage, {"width": 640}) == (640, 360)


@pytest.mark.asyncio
async def test_resize_by_height_alone_preserves_aspect(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    assert await _apply(ResizeWorker(), storage, {"height": 360}) == (640, 360)


@pytest.mark.asyncio
async def test_resize_forces_even_dimensions(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """641 is unencodable in yuv420p; it must be rounded rather than fail."""
    width, height = await _apply(ResizeWorker(), storage, {"width": 641})
    assert width == 640 and height % 2 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{}, {"width": 0}, {"height": -100}, {"width": 99999}, {"width": "big"},
     {"width": True}, {"width": 640.5}, {"scale": 2}],
)
async def test_resize_rejects_bad_params(storage, params: dict) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await ResizeWorker().process(_message(source, params), None)
    assert isinstance(exc_info.value, PermanentError)


# --- rotate ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_90_swaps_dimensions(ffmpeg_available, ffprobe_available, storage) -> None:
    assert await _apply(RotateWorker(), storage, {"degrees": 90}) == (720, 1280)


@pytest.mark.asyncio
async def test_rotate_270_swaps_dimensions(ffmpeg_available, ffprobe_available, storage) -> None:
    assert await _apply(RotateWorker(), storage, {"degrees": 270}) == (720, 1280)


@pytest.mark.asyncio
async def test_rotate_180_keeps_dimensions(ffmpeg_available, ffprobe_available, storage) -> None:
    """180 is hflip+vflip rather than two transposes, so the frame must come
    back the same shape it went in."""
    assert await _apply(RotateWorker(), storage, {"degrees": 180}) == (1280, 720)


@pytest.mark.asyncio
@pytest.mark.parametrize("degrees", [45, 0, 360, -90, "90", 90.0, True, None])
async def test_rotate_rejects_unsupported_angles(storage, degrees) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await RotateWorker().process(_message(source, {"degrees": degrees}), None)
    assert isinstance(exc_info.value, PermanentError)


# --- flip ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["horizontal", "vertical", "HORIZONTAL"])
async def test_flip_keeps_dimensions(
    ffmpeg_available, ffprobe_available, storage, direction: str
) -> None:
    assert await _apply(FlipWorker(), storage, {"direction": direction}) == (1280, 720)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["sideways", "", None, 1, True])
async def test_flip_rejects_bad_direction(storage, direction) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await FlipWorker().process(_message(source, {"direction": direction}), None)
    assert isinstance(exc_info.value, PermanentError)


# --- pad -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pad_to_vertical_keeps_full_width(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The whole point of pad vs crop: nothing is cut off, so the original
    1280 width survives and bars are added above and below."""
    width, height = await _apply(PadWorker(), storage, {"aspect_ratio": "9:16"})

    assert width == 1280, "pad must not lose any of the picture"
    assert width / height == pytest.approx(9 / 16, abs=0.01)


@pytest.mark.asyncio
async def test_pad_to_square(ffmpeg_available, ffprobe_available, storage) -> None:
    assert await _apply(PadWorker(), storage, {"aspect_ratio": "1:1"}) == (1280, 1280)


@pytest.mark.asyncio
async def test_pad_accepts_a_colour(ffmpeg_available, ffprobe_available, storage) -> None:
    width, height = await _apply(
        PadWorker(), storage, {"aspect_ratio": "1:1", "pad_color": "white"}
    )
    assert (width, height) == (1280, 1280)


@pytest.mark.asyncio
async def test_pad_matching_ratio_is_a_no_op(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    assert await _apply(PadWorker(), storage, {"aspect_ratio": "16:9"}) == (1280, 720)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"aspect_ratio": "banana"},
        {"aspect_ratio": "9:0"},
        {"aspect_ratio": 1.7},
        {"aspect_ratio": "1:1", "pad_color": "red:evil"},   # filtergraph delimiter
        {"aspect_ratio": "1:1", "pad_color": "a,b"},        # filter separator
        {"aspect_ratio": "1:1", "pad_color": ""},
        {"aspect_ratio": "1:1", "colour": "red"},           # unknown param
    ],
)
async def test_pad_rejects_bad_params(storage, params: dict) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await PadWorker().process(_message(source, params), None)
    assert isinstance(exc_info.value, PermanentError)


# --- crop vs pad, the distinction that matters -----------------------------


@pytest.mark.asyncio
async def test_crop_and_pad_reach_the_same_ratio_by_opposite_means(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Both hit 9:16. crop keeps the height and loses width; pad keeps the
    width and gains height. Getting these backwards would silently destroy
    the edges of someone's footage."""
    from backend.workers.crop_worker import CropWorker

    cropped = await _apply(CropWorker(), storage, {"aspect_ratio": "9:16"})
    padded = await _apply(PadWorker(), storage, {"aspect_ratio": "9:16"})

    assert cropped[0] < 1280 and cropped[1] == 720, "crop narrows, keeps height"
    assert padded[0] == 1280 and padded[1] > 720, "pad keeps width, grows height"
    for width, height in (cropped, padded):
        assert width / height == pytest.approx(9 / 16, abs=0.01)


# --- chaining --------------------------------------------------------------


@pytest.mark.asyncio
async def test_transforms_chain_and_forward_other_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """rotate 90 then resize: the resize must act on the rotated 720x1280
    intermediate, and an unrelated srt must survive both stages."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    rotated = await RotateWorker().process(_message(source, {"degrees": 90}), None)
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")
    previous = {"assets": [*rotated["assets"], srt.to_dict()]}

    payload = await ResizeWorker().process(
        _message(source, {"height": 640}, stage=1), previous
    )

    assets = previous_assets(payload)
    assert srt in assets
    # 720x1280 scaled to height 640 -> width 360. Had it re-read the
    # original 1280x720, height 640 would have given width 1138.
    assert await _dimensions(primary_video(assets).uri) == (360, 640)
