"""Job submission and lookup."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas.artifact import (
    ArtifactResponse,
    JobArtifactsResponse,
    StageArtifactsResponse,
)
from backend.api.schemas.job import JobCreateRequest, JobResponse, MemoryUpdateResponse
from backend.api.deps import CurrentUserDep, SessionDep
from backend.repositories.job_repository import JobRepository
from backend.repositories.project_job_repository import ProjectJobRepository
from backend.repositories.project_member_repository import ProjectMemberRepository
from backend.repositories.result_repository import ResultRepository
from backend.services.job_submission_service import (
    IdempotencyConflictError,
    JobSubmissionService,
)
from backend.services.memory_update_service import (
    JobNotFoundError,
    MemoryUpdateService,
)
from backend.services.planner_factory import get_default_memory_extractor

# The one canonical parser for the asset payload shape lives in media.py.
# StageProcessingService deliberately abstains from importing it (Setu's
# engine stays payload-opaque); the API layer sits above the workers, so
# it has no such constraint and reuses it rather than re-deriving the
# shape a third time.
from backend.workers.media import previous_assets

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    session: SessionDep,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1),
) -> JobResponse:
    service = JobSubmissionService(session)
    try:
        result = await service.submit(
            idempotency_key=idempotency_key,
            workflow=body.workflow,
            payload=body.payload,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return JobResponse.from_model(result.job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: uuid.UUID, session: SessionDep) -> JobResponse:
    job = await JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobResponse.from_model(job)


@router.get("/{job_id}/artifacts", response_model=JobArtifactsResponse)
async def get_job_artifacts(job_id: uuid.UUID, session: SessionDep) -> JobArtifactsResponse:
    """What each stage of this job produced.

    Listed per stage rather than as a flat set of final outputs: every
    intermediate already persists, so this doubles as the "which step got
    it wrong" view Phase 7 needs, not just a download link for the last
    one. Stages that produced no assets (dummy, video_analysis, and any
    pre-Phase-5 worker) appear with an empty list rather than being
    hidden, so the response still describes the whole workflow.
    """
    job = await JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    results = await ResultRepository(session).list_by_job(job_id)
    return JobArtifactsResponse(
        job_id=job.id,
        status=job.status,
        stages=[
            StageArtifactsResponse(
                stage=result.stage,
                worker_name=result.worker_name,
                artifacts=[
                    ArtifactResponse.from_asset(kind=asset.kind, uri=asset.uri, job_id=job.id)
                    for asset in previous_assets(result.payload)
                ],
            )
            for result in results
        ],
    )


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> JobResponse:
    """Stop a running job between stages. Job owner only.

    **Cooperative, not forceful.** The stage currently in flight runs to
    completion and keeps its Result -- there is no mid-stage kill -- and
    the workflow simply stops rather than dispatching the next stage. So a
    cancel during a long render is not instant; it takes effect at the
    next stage boundary.

    Authorized against `project_jobs.submitted_by_user_id`, the column
    Phase 8 recorded at submission time. That is the *job* owner --
    whoever's action started the work -- which is deliberately not the
    room's owner and not the `admin` approver from Phase 9a. A member who
    is not the job's owner gets 403: they can already see the job, so
    there is nothing left to hide and "you are not allowed" is the more
    useful answer. Anyone outside the room gets 404, matching every other
    room-scoped endpoint.
    """
    owner_id = await ProjectJobRepository(session).owner_of_job(job_id)
    project_id = await ProjectJobRepository(session).project_for_job(job_id)
    if owner_id is None or project_id is None:
        # No project_jobs row: either no such job, or a raw POST /jobs
        # submission that belongs to no room. Neither has an owner to
        # authorize against, and both are indistinguishable from outside.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    if not await ProjectMemberRepository(session).is_member(project_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    if owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the job owner can cancel this job",
        )

    if not await JobRepository(session).cancel(job_id):
        # Already finished, dead-lettered, or cancelled. A 409 rather than
        # a silent success: the caller asked to stop something that had
        # already stopped, and pretending otherwise would have them show a
        # user "cancelling..." forever.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="job has already finished"
        )
    await session.commit()

    job = await JobRepository(session).get(job_id)
    await session.refresh(job)
    return JobResponse.from_model(job)


@router.post("/{job_id}/update-memory", response_model=MemoryUpdateResponse)
async def update_memory(job_id: uuid.UUID, session: SessionDep) -> MemoryUpdateResponse:
    """Learn any durable preferences from the conversation behind a
    finished job.

    Called explicitly by the client once its normal GET /jobs/{id} poll
    shows `completed` — rather than triggered from that GET, which stays
    read-only with no hidden side effects.

    Safe to call repeatedly: `conversations.memory_processed_at` gates the
    work, so a double-poll, a retry or a second browser tab all land on
    the same outcome and only the first pays for an LLM call. A failed
    extraction still returns success, because the *job* succeeded and
    failing to learn from it is not the user's problem.
    """
    try:
        result = await MemoryUpdateService(
            session, get_default_memory_extractor()
        ).update_from_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        ) from exc
    return MemoryUpdateResponse(
        processed=result.processed, updated_fields=result.updated_fields
    )
