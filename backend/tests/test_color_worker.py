"""ColorWorker (Phase 5C).

Picture measurements come from ffmpeg's own signalstats filter rather than
a Python imaging library — the project has no numpy/Pillow/OpenCV
dependency and this avoids adding one just to assert a frame got brighter.

tests/fixtures/sample.mp4 is ffmpeg's `testsrc` pattern: strongly coloured
bars, which makes saturation and brightness changes easy to measure.
"""

import re
import uuid
from pathlib import Path

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.base import PermanentError, StageMessage
from backend.workers.color_worker import ColorWorker, _build_filters
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    materialize_to_tempfile,
    previous_assets,
    primary_video,
    run_ffmpeg,
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
        workflow=["color"],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": [source_uri]}}},
    )


async def _signalstat(uri: str, key: str) -> float:
    """Mean of a signalstats metric across the clip's frames.

    YAVG is average luma (brightness); SATAVG is average saturation.
    """
    with materialize_to_tempfile(uri) as path:
        stderr = await run_ffmpeg(
            [
                "-i", str(path),
                "-vf", f"signalstats,metadata=print:key=lavfi.signalstats.{key}",
                "-f", "null", "-",
            ]
        )
    values = [float(m) for m in re.findall(rf"signalstats\.{key}=([\d.]+)", stderr)]
    assert values, f"no {key} samples parsed from ffmpeg output"
    return sum(values) / len(values)


async def _run(storage: LocalDiskStorage, params: dict) -> str:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    payload = await ColorWorker().process(_message(source, params), None)
    return primary_video(previous_assets(payload)).uri


# --- filter building (pure) ------------------------------------------------


def test_emits_only_the_requested_adjustments() -> None:
    """An unmentioned channel must be left alone, not restated at neutral —
    otherwise every color stage would touch everything."""
    assert _build_filters({"brightness": 0.3}) == ["eq=brightness=0.3"]


def test_combines_eq_terms_into_one_filter() -> None:
    filters = _build_filters({"brightness": 0.2, "contrast": 1.4, "saturation": 1.5})

    assert len(filters) == 1
    assert filters[0].startswith("eq=")
    for term in ("brightness=0.2", "contrast=1.4", "saturation=1.5"):
        assert term in filters[0]


def test_sharpen_is_a_separate_filter_from_eq() -> None:
    filters = _build_filters({"brightness": 0.2, "sharpen": 1.0})

    assert "eq=brightness=0.2" in filters
    assert any(f.startswith("unsharp=") for f in filters)


def test_accepts_ints_where_floats_are_expected() -> None:
    """JSON has one number type and a planner writes 1 as readily as 1.0."""
    assert _build_filters({"contrast": 2}) == ["eq=contrast=2.0"]


@pytest.mark.parametrize(
    "params",
    [
        {},                              # nothing asked for
        {"brightness": 0.0},             # neutral value == no-op
        {"contrast": 1.0, "gamma": 1.0},  # all neutral
        {"brightness": 5.0},             # out of range
        {"saturation": -1.0},
        {"gamma": 0.0},
        {"sharpen": 99.0},
        {"brightness": "bright"},        # not a number
        {"contrast": True},              # bool is an int subclass; not an adjustment
        {"vibrance": 1.2},               # unknown param
    ],
)
def test_rejects_unusable_requests_permanently(params: dict) -> None:
    with pytest.raises(InvalidMediaParamsError) as exc_info:
        _build_filters(params)

    assert isinstance(exc_info.value, PermanentError)


# --- real encodes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_brightness_increase_is_measurably_brighter(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    before = await _signalstat(source, "YAVG")

    brightened = await _run(storage, {"brightness": 0.3})

    assert await _signalstat(brightened, "YAVG") > before


@pytest.mark.asyncio
async def test_brightness_decrease_is_measurably_darker(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The opposite direction, so the test can't pass just because any
    re-encode happens to raise the measured average."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    before = await _signalstat(source, "YAVG")

    darkened = await _run(storage, {"brightness": -0.3})

    assert await _signalstat(darkened, "YAVG") < before


@pytest.mark.asyncio
async def test_desaturating_reduces_measured_saturation(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    before = await _signalstat(source, "SATAVG")

    grey = await _run(storage, {"saturation": 0.0})

    assert await _signalstat(grey, "SATAVG") < before


@pytest.mark.asyncio
async def test_geometry_is_untouched(ffmpeg_available, ffprobe_available, storage) -> None:
    """color adjusts pixels, never the frame — a regression here would mean
    a colour tweak silently resized someone's video."""
    from backend.workers.media import probe, video_stream

    graded = await _run(storage, {"contrast": 1.4})

    with materialize_to_tempfile(graded) as path:
        stream = video_stream(await probe(path))
    assert (stream["width"], stream["height"]) == (1280, 720)


@pytest.mark.asyncio
async def test_chains_and_forwards_other_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="sample.mp4")
    first = await ColorWorker().process(_message(source, {"brightness": 0.2}), None)
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")
    previous = {"assets": [*first["assets"], srt.to_dict()]}

    payload = await ColorWorker().process(
        _message(source, {"saturation": 1.4}, stage=1), previous
    )

    assets = previous_assets(payload)
    assert srt in assets
    assert primary_video(assets).uri not in {source, primary_video(previous_assets(first)).uri}


@pytest.mark.asyncio
async def test_bad_params_fail_before_any_download(storage) -> None:
    """No ffmpeg fixture: validation happens before the input is fetched, so
    a hopeless request costs nothing."""
    source = storage.put(b"not a video", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError):
        await ColorWorker().process(_message(source, {"brightness": 99.0}), None)
