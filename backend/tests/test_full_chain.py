"""The Phase 5 capstone: every capability in one workflow.

Each worker is tested in isolation elsewhere. What this file proves is the
thing isolation cannot — that they compose. A seven-stage chain exercises
the asset model end to end: the video is edited by stage after stage, an
srt produced in the middle survives to the end, and `render` operates on
everything that came before rather than re-reading the original upload.

Deliberately driven through the workers directly rather than Kafka: this
is about composition, and the execution machinery already has its own
integration tests (test_workflow_engine.py).
"""

import uuid
from pathlib import Path

import pytest

from backend.services.capability_registry import DEFAULT_CAPABILITY_REGISTRY
from backend.services.proposal import Proposal, ProposalStage
from backend.services.proposal_validator import validate_proposal
from backend.services.transcription_client import (
    TranscriptionClient,
    TranscriptionResult,
    TranscriptSegment,
)
from backend.storage.local import LocalDiskStorage
from backend.workers.audio_worker import AudioWorker
from backend.workers.base import StageMessage
from backend.workers.color_worker import ColorWorker
from backend.workers.crop_worker import CropWorker
from backend.workers.media import (
    AssetKind,
    materialize_to_tempfile,
    previous_assets,
    primary_video,
    probe,
    video_stream,
)
from backend.workers.render_worker import RenderWorker
from backend.workers.subtitle_burn_worker import SubtitleBurnWorker
from backend.workers.transcribe_worker import TranscribeWorker
from backend.workers.trim_worker import TrimWorker

_SAMPLE = Path(__file__).parent / "fixtures" / "sample_with_audio.mp4"  # 320x240, 4s, audio


class _Transcriber(TranscriptionClient):
    async def transcribe(self, audio, *, filename, language=None):
        return TranscriptionResult(
            text="Hello from Setu",
            language="en",
            segments=[TranscriptSegment(start=0.0, end=2.0, text="Hello from Setu")],
        )


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    monkeypatch.setattr("backend.storage.get_storage", lambda: disk)
    return disk


# The workflow a real "make this ready to post" request would compile to.
_WORKFLOW = [
    ("trim", {"end": 3.0}, TrimWorker()),
    ("audio", {"normalize": True}, AudioWorker()),
    ("transcribe", {}, TranscribeWorker(_Transcriber())),
    ("crop", {"aspect_ratio": "9:16"}, CropWorker()),
    ("color", {"brightness": 0.1, "saturation": 1.2}, ColorWorker()),
    ("burn_subtitles", {"style": "default"}, SubtitleBurnWorker()),
    ("render", {"format": "mp4", "resolution": "480p"}, RenderWorker()),
]


def _message(stage: int, params: dict, source_uri: str) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=[name for name, _, _ in _WORKFLOW],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": [source_uri]}}},
    )


@pytest.mark.asyncio
async def test_seven_stage_chain_composes(ffmpeg_available, ffprobe_available, storage):
    """trim → audio → transcribe → crop → color → burn_subtitles → render."""
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="raw.mp4")

    previous: dict | None = None
    produced_uris: list[str] = []
    for stage, (name, params, worker) in enumerate(_WORKFLOW):
        previous = await worker.process(_message(stage, params, source), previous)
        video = primary_video(previous_assets(previous))
        assert video is not None, f"stage {stage} ({name}) produced no video"
        produced_uris.append(video.uri)

    assets = previous_assets(previous)
    final = primary_video(assets)

    # 1. The srt produced at stage 2 survived four more stages to the end.
    assert any(a.kind == AssetKind.SRT for a in assets), "srt lost along the chain"
    assert any(a.kind == AssetKind.TRANSCRIPT for a in assets)

    # 2. The final artifact carries every edit, not just the last one.
    with materialize_to_tempfile(final.uri) as path:
        data = await probe(path)
    stream = video_stream(data)
    assert float(data["format"]["duration"]) == pytest.approx(3.0, abs=0.3), (
        "the stage-0 trim must still be present in the final render"
    )
    assert stream["height"] == 480, "render's resolution applied"
    assert stream["width"] / stream["height"] == pytest.approx(9 / 16, abs=0.02), (
        "crop's aspect ratio survived through to the render"
    )
    assert "h264" in {s.get("codec_name") for s in data["streams"]}

    # 3. Every stage stored a distinct object — nothing silently no-opped
    #    by handing its input straight back, except transcribe, which is
    #    defined to pass the video through untouched.
    assert produced_uris[2] == produced_uris[1], "transcribe must not re-encode"
    editing_stages = [u for i, u in enumerate(produced_uris) if i != 2]
    assert len(set(editing_stages)) == len(editing_stages), "a stage reused its input"


@pytest.mark.asyncio
async def test_the_chain_would_pass_proposal_validation() -> None:
    """The same workflow, checked against the real registry — so the chain
    above is one the planner could actually have proposed, not merely one
    the workers happen to accept."""
    proposal = Proposal(
        summary="Make this ready to post",
        workflow=[
            ProposalStage(stage=name, video_ids=["video_1"], params=params)
            for name, params, _ in _WORKFLOW
        ],
    )

    result = validate_proposal(
        proposal, DEFAULT_CAPABILITY_REGISTRY, known_video_handles=frozenset({"video_1"})
    )

    assert result.valid, result.errors


@pytest.mark.asyncio
async def test_burn_before_transcribe_is_rejected_by_validation() -> None:
    """The ordering guard, on the real registry: reversing the two must
    fail at proposal time rather than at execution, so the planner can fix
    it instead of the job dying in the DLQ."""
    proposal = Proposal(
        summary="captions",
        workflow=[
            ProposalStage(stage="burn_subtitles", video_ids=["video_1"]),
            ProposalStage(stage="transcribe", video_ids=["video_1"]),
        ],
    )

    result = validate_proposal(
        proposal, DEFAULT_CAPABILITY_REGISTRY, known_video_handles=frozenset({"video_1"})
    )

    assert not result.valid
    assert any("srt" in error for error in result.errors)
