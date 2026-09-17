"""SceneDetectionWorker (Phase 10)."""

import json
import uuid
from pathlib import Path

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.base import StageMessage
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    primary_video,
    run_ffmpeg,
)
from backend.workers.scene_detection_worker import SceneDetectionWorker

_FIXTURES = Path(__file__).parent / "fixtures"
_SILENT = _FIXTURES / "sample.mp4"


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


@pytest.fixture
async def hard_cut_clip(ffmpeg_available) -> bytes:
    """A 2s clip, red for the first second and blue for the second --
    ffmpeg's own scene filter should find one unambiguous cut at ~1s.
    Generated on the fly rather than checked in, matching sample.mp4's
    own origin (ffmpeg's testsrc) but built here specifically to
    guarantee a detectable cut, which no existing fixture promises."""
    with output_tempfile(".mp4") as destination:
        await run_ffmpeg(
            [
                "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1:r=24",
                "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1:r=24",
                "-filter_complex", "concat=n=2:v=1:a=0",
                str(destination),
            ]
        )
        return destination.read_bytes()


def _message(
    uris: list[str], params: dict, stage: int = 0, video_ids: list[str] | None = None
) -> StageMessage:
    entry = {"params": params, "video_uris": uris}
    if video_ids is not None:
        entry["video_ids"] = video_ids
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["detect_scenes"],
        payload={"stage_params": {str(stage): entry}},
    )


# --- detection ---------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_cuts_is_valid_not_an_error(ffmpeg_available, storage) -> None:
    """A single continuous shot legitimately has no cuts -- must not be
    treated as a failure."""
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")

    payload = await SceneDetectionWorker().process(_message([source], {}), None)

    scenes = next(a for a in previous_assets(payload) if a.kind == AssetKind.SCENES)
    data = storage.get(scenes.uri)
    assert json.loads(data)["cuts"] == []


@pytest.mark.asyncio
async def test_a_real_cut_is_detected(ffmpeg_available, storage, hard_cut_clip) -> None:
    source = storage.put(hard_cut_clip, suggested_name="cut.mp4")

    payload = await SceneDetectionWorker().process(_message([source], {}), None)

    scenes = next(a for a in previous_assets(payload) if a.kind == AssetKind.SCENES)
    cuts = json.loads(storage.get(scenes.uri))["cuts"]
    assert len(cuts) >= 1
    assert any(0.7 <= c["time"] <= 1.3 for c in cuts)


@pytest.mark.asyncio
async def test_video_passed_through_untouched(ffmpeg_available, storage) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")

    payload = await SceneDetectionWorker().process(_message([source], {}), None)

    assert primary_video(previous_assets(payload)).uri == source


@pytest.mark.asyncio
async def test_rejects_unknown_params(storage) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError):
        await SceneDetectionWorker().process(_message([source], {"sensitivity": 0.5}), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0.0, -0.1, 1.1, "high", True])
async def test_rejects_bad_threshold(storage, bad) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError):
        await SceneDetectionWorker().process(_message([source], {"threshold": bad}), None)


# --- caching (Phase 10 foundation, generalized) -------------------------


@pytest.mark.asyncio
async def test_cache_hit_skips_ffmpeg_and_still_forwards_video_asset(
    storage, monkeypatch
) -> None:
    async def _boom(*args, **kwargs):
        raise AssertionError("ffmpeg must not run on a cache hit")

    monkeypatch.setattr("backend.workers.scene_detection_worker.run_ffmpeg", _boom)

    source = storage.put(b"x", suggested_name="x.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cached_scenes = Asset(kind=AssetKind.SCENES, uri="local://cached-scenes.json")
    cache.seed(video_id, AssetKind.SCENES, cached_scenes)

    payload = await SceneDetectionWorker(cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    assets = previous_assets(payload)
    assert primary_video(assets).uri == source
    assert next(a for a in assets if a.kind == AssetKind.SCENES).uri == cached_scenes.uri


@pytest.mark.asyncio
async def test_cache_miss_writes_through(ffmpeg_available, storage) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()

    payload = await SceneDetectionWorker(cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    assert cache.put_calls == [(video_id, AssetKind.SCENES)]
    produced = next(a for a in previous_assets(payload) if a.kind == AssetKind.SCENES)
    assert cache.store[(video_id, AssetKind.SCENES)].uri == produced.uri


@pytest.mark.asyncio
async def test_cache_is_bypassed_for_a_threshold_override(ffmpeg_available, storage) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.seed(
        video_id, AssetKind.SCENES, Asset(kind=AssetKind.SCENES, uri="local://should-not-be-used.json")
    )

    await SceneDetectionWorker(cache=cache).process(
        _message([source], {"threshold": 0.6}, video_ids=[video_id]), None
    )

    assert cache.get_calls == []
    assert cache.put_calls == []


@pytest.mark.asyncio
async def test_cache_is_bypassed_once_a_prior_stage_produced_a_video(
    ffmpeg_available, storage
) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.seed(
        video_id, AssetKind.SCENES, Asset(kind=AssetKind.SCENES, uri="local://should-not-be-used.json")
    )
    previous_output = {"assets": [Asset(kind=AssetKind.VIDEO, uri=source).to_dict()]}

    await SceneDetectionWorker(cache=cache).process(
        _message([source], {}, stage=1, video_ids=[video_id]), previous_output
    )

    assert cache.get_calls == []
    assert cache.put_calls == []


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_fail_the_stage(ffmpeg_available, storage) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.fail_puts_with(RuntimeError("db unreachable"))

    payload = await SceneDetectionWorker(cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "scenes"}


@pytest.mark.asyncio
async def test_cache_read_failure_falls_back_to_real_detection(ffmpeg_available, storage) -> None:
    source = storage.put(_SILENT.read_bytes(), suggested_name="clip.mp4")
    video_id = str(uuid.uuid4())
    cache = FakeVideoAssetCache()
    cache.fail_gets_with(RuntimeError("db unreachable"))

    payload = await SceneDetectionWorker(cache=cache).process(
        _message([source], {}, video_ids=[video_id]), None
    )

    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "scenes"}
