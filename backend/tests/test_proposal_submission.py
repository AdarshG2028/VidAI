"""Phase 3's one integration test: a hand-authored proposal, validated and
compiled, submitted through JobSubmissionService completely unmodified, and
run to completion on the existing `dummy` worker -- proving the pipeline
end to end without any Setu-core change.

No real Kafka needed: StageProcessingService is called directly, the same
pattern test_workflow_engine.py already uses for a single-stage job (the
harness/Kafka layer is exercised elsewhere, not the concern here).
"""

import uuid

import pytest

from backend.models import JobStatus
from backend.repositories.job_repository import JobRepository
from backend.services.job_submission_service import JobSubmissionService
from backend.services.proposal import Proposal
from backend.services.proposal_validator import validate_proposal
from backend.services.stage_processing_service import (
    ProcessingOutcome,
    StageProcessingService,
)
from backend.services.workflow_compiler import ExecutionContext, compile_workflow
from backend.workers.base import StageMessage
from backend.workers.dummy_worker import DummyWorker

pytestmark = pytest.mark.usefixtures("database_url")


@pytest.mark.asyncio
async def test_valid_proposal_runs_to_completion_through_the_full_pipeline(session) -> None:
    proposal = Proposal.from_dict(
        {
            "summary": "Prove the proposal pipeline end to end.",
            "workflow": [{"stage": "dummy", "params": {}}],
        }
    )

    validation = validate_proposal(proposal)
    assert validation.valid is True, validation.errors

    workflow, payload = compile_workflow(
        proposal, ExecutionContext(video_uris={})
    )
    assert workflow == ["dummy"]

    submission = await JobSubmissionService(session).submit(
        idempotency_key=f"test-proposal:{uuid.uuid4()}",
        workflow=workflow,
        payload=payload,
    )
    job_id = submission.job.id

    outcome = await StageProcessingService(session, DummyWorker()).handle(
        StageMessage(job_id=job_id, stage=0, workflow=workflow, payload=payload)
    )
    assert outcome.outcome == ProcessingOutcome.SUCCEEDED

    job = await JobRepository(session).get(job_id)
    assert job.status == JobStatus.COMPLETED
