"""FillerWordWorker (Phase 10)."""

import json
import uuid

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.base import StageMessage
from backend.workers.filler_word_worker import FillerWordWorker
from backend.workers.media import Asset, AssetKind, InvalidMediaParamsError, previous_assets


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    monkeypatch.setattr("backend.workers.filler_word_worker.get_storage", lambda: disk)
    monkeypatch.setattr("backend.storage.get_storage", lambda: disk)
    return disk


def _message(params: dict | None = None, stage: int = 1) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["transcribe", "detect_filler_words"],
        payload={"stage_params": {str(stage): {"params": params or {}, "video_uris": []}}},
    )


def _transcript_previous_output(storage: LocalDiskStorage, segments: list[dict]) -> dict:
    uri = storage.put(
        json.dumps({"text": "", "language": "en", "segments": segments}).encode("utf-8"),
        suggested_name="transcript.json",
    )
    return {
        "assets": [
            Asset(kind=AssetKind.VIDEO, uri="local://video.mp4").to_dict(),
            Asset(kind=AssetKind.TRANSCRIPT, uri=uri).to_dict(),
            Asset(kind=AssetKind.SRT, uri="local://captions.srt").to_dict(),
        ]
    }


@pytest.mark.asyncio
async def test_detects_unambiguous_fillers(storage) -> None:
    previous_output = _transcript_previous_output(
        storage,
        [{"start": 0.0, "end": 3.0, "text": "Well, um, I think, uh, we should go."}],
    )

    payload = await FillerWordWorker().process(_message(), previous_output)

    filler_asset = next(a for a in previous_assets(payload) if a.kind == AssetKind.FILLER_WORDS)
    instances = json.loads(storage.get(filler_asset.uri))["instances"]
    words = [i["word"].lower() for i in instances]
    assert words == ["um", "uh"]
    assert all(i["segment_start"] == 0.0 and i["segment_end"] == 3.0 for i in instances)


@pytest.mark.asyncio
async def test_ignores_ambiguous_words(storage) -> None:
    """'like', 'so', 'actually' are filler maybe a third of the time --
    deliberately not flagged, unlike unambiguous 'um'/'uh'."""
    previous_output = _transcript_previous_output(
        storage,
        [{"start": 0.0, "end": 2.0, "text": "I like it, so yeah, actually great."}],
    )

    payload = await FillerWordWorker().process(_message(), previous_output)

    filler_asset = next(a for a in previous_assets(payload) if a.kind == AssetKind.FILLER_WORDS)
    assert json.loads(storage.get(filler_asset.uri))["instances"] == []


@pytest.mark.asyncio
async def test_case_insensitive_and_repeated_letters(storage) -> None:
    previous_output = _transcript_previous_output(
        storage, [{"start": 0.0, "end": 1.0, "text": "Ummm, that's tricky. Uh huh."}]
    )

    payload = await FillerWordWorker().process(_message(), previous_output)

    filler_asset = next(a for a in previous_assets(payload) if a.kind == AssetKind.FILLER_WORDS)
    instances = json.loads(storage.get(filler_asset.uri))["instances"]
    assert [i["word"].lower() for i in instances] == ["ummm", "uh"]


@pytest.mark.asyncio
async def test_no_false_positive_inside_ordinary_words(storage) -> None:
    """'thumb' contains the letters 'umb' -- word-boundary matching must
    not flag it as a filler."""
    previous_output = _transcript_previous_output(
        storage, [{"start": 0.0, "end": 1.0, "text": "I hurt my thumb yesterday."}]
    )

    payload = await FillerWordWorker().process(_message(), previous_output)

    filler_asset = next(a for a in previous_assets(payload) if a.kind == AssetKind.FILLER_WORDS)
    assert json.loads(storage.get(filler_asset.uri))["instances"] == []


@pytest.mark.asyncio
async def test_no_speech_at_all_produces_no_instances(storage) -> None:
    previous_output = _transcript_previous_output(storage, [])

    payload = await FillerWordWorker().process(_message(), previous_output)

    filler_asset = next(a for a in previous_assets(payload) if a.kind == AssetKind.FILLER_WORDS)
    assert json.loads(storage.get(filler_asset.uri))["instances"] == []


@pytest.mark.asyncio
async def test_missing_transcript_fails_clearly(storage) -> None:
    previous_output = {
        "assets": [Asset(kind=AssetKind.VIDEO, uri="local://video.mp4").to_dict()]
    }

    with pytest.raises(InvalidMediaParamsError, match="transcribe"):
        await FillerWordWorker().process(_message(), previous_output)


@pytest.mark.asyncio
async def test_no_previous_output_fails_clearly(storage) -> None:
    with pytest.raises(InvalidMediaParamsError, match="transcribe"):
        await FillerWordWorker().process(_message(), None)


@pytest.mark.asyncio
async def test_forwards_all_carried_assets_plus_filler_words(storage) -> None:
    previous_output = _transcript_previous_output(
        storage, [{"start": 0.0, "end": 1.0, "text": "um"}]
    )

    payload = await FillerWordWorker().process(_message(), previous_output)

    kinds = {a.kind for a in previous_assets(payload)}
    assert kinds == {"video", "transcript", "srt", "filler_words"}


@pytest.mark.asyncio
async def test_rejects_unexpected_params(storage) -> None:
    previous_output = _transcript_previous_output(
        storage, [{"start": 0.0, "end": 1.0, "text": "um"}]
    )

    with pytest.raises(InvalidMediaParamsError):
        await FillerWordWorker().process(_message({"sensitivity": "high"}), previous_output)
