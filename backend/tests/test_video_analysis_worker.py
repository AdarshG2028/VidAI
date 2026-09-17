"""VideoAnalysisWorker: exception translation from backend.workers.media's
generic ffprobe/storage layer into this worker's own retry/DLQ contract.

Pure metadata-parsing (extract_video_metadata, _parse_duration, etc.) is
tested in tests/test_media_helpers.py now that it lives in
backend/workers/media.py, shared with video_chain.measure()."""

import uuid
from pathlib import Path

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.base import PermanentError, StageMessage
from backend.workers.video_analysis_worker import (
    UnsupportedVideoError,
    VideoAnalysisError,
    VideoAnalysisWorker,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_process_raises_video_analysis_error_when_ffprobe_missing(
    tmp_path, monkeypatch
) -> None:
    """Without needing ffprobe installed: force the "not on PATH" branch and
    confirm it surfaces as VideoAnalysisError, not a raw FileNotFoundError —
    that's what lets the harness's normal retry/DLQ path handle it. Also
    confirm it is NOT a PermanentError: ffprobe missing is an environment
    problem, not an input problem, so it must stay retryable — a redeploy
    or a retry landing on a different worker instance could still fix it."""
    storage = LocalDiskStorage(tmp_path)
    uri = storage.put(b"not really a video", suggested_name="clip.mp4")
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: storage)

    async def _raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(
        "backend.workers.media.asyncio.create_subprocess_exec", _raise_not_found
    )

    worker = VideoAnalysisWorker()
    message = StageMessage(
        job_id=uuid.uuid4(),
        stage=0,
        workflow=["video_analysis"],
        payload={"video_id": "irrelevant", "video_uri": uri},
    )

    with pytest.raises(VideoAnalysisError) as exc_info:
        await worker.process(message, None)
    assert not isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_process_raises_unsupported_video_error_for_non_video_file(
    ffprobe_available, tmp_path, monkeypatch
) -> None:
    """The reproduction case for the retry-waste bug: a non-video file
    (e.g. a .txt uploaded as a video) makes ffprobe exit non-zero on every
    identical redelivery. Confirm it's the PermanentError subclass so the
    harness DLQs on the first attempt instead of the full retry budget."""
    storage = LocalDiskStorage(tmp_path)
    uri = storage.put(b"not a video, just text", suggested_name="not-a-video.mp4")
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: storage)

    worker = VideoAnalysisWorker()
    message = StageMessage(
        job_id=uuid.uuid4(),
        stage=0,
        workflow=["video_analysis"],
        payload={"video_id": "irrelevant", "video_uri": uri},
    )

    with pytest.raises(UnsupportedVideoError) as exc_info:
        await worker.process(message, None)
    assert isinstance(exc_info.value, PermanentError)


@pytest.mark.asyncio
async def test_process_extracts_real_metadata_from_sample_video(
    ffprobe_available, tmp_path, monkeypatch
) -> None:
    """The one test that exercises the real ffprobe subprocess path, not a
    mock — only runs when ffprobe is on PATH (see conftest.py). Regenerate
    the fixture with:
    ffmpeg -f lavfi -i "testsrc=size=1280x720:rate=30" -t 3 -pix_fmt yuv420p tests/fixtures/sample.mp4
    """
    sample = _FIXTURES_DIR / "sample.mp4"
    storage = LocalDiskStorage(tmp_path)
    uri = storage.put(sample.read_bytes(), suggested_name="sample.mp4")
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: storage)

    worker = VideoAnalysisWorker()
    message = StageMessage(
        job_id=uuid.uuid4(),
        stage=0,
        workflow=["video_analysis"],
        payload={"video_id": "irrelevant", "video_uri": uri},
    )
    metadata = await worker.process(message, None)

    assert metadata == {
        "duration_seconds": 3.0,
        "fps": 30.0,
        "width": 1280,
        "height": 720,
        "resolution": "1280x720",
        "orientation": "landscape",
        "codec": "h264",
    }
