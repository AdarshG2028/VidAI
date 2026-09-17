"""Video lookup by id — Job #1 (video_analysis) end to end.

Creation lives under /projects/{project_id}/videos (routes/projects.py):
every video belongs to a project, so it's created there. Reading a video by
its own id is still a reasonable flat lookup, so GET stays here.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas.video import VideoDetailResponse
from backend.api.deps import SessionDep
from backend.models import JobStatus
from backend.repositories.job_repository import JobRepository
from backend.repositories.result_repository import ResultRepository
from backend.repositories.video_repository import VideoRepository

router = APIRouter(prefix="/videos", tags=["videos"])

# video_analysis is always stage 0 of Job #1 — a single-stage workflow.
_ANALYSIS_STAGE = 0
_TERMINAL_FAILURE_STATUSES = {JobStatus.FAILED, JobStatus.DEAD_LETTERED, JobStatus.CANCELLED}


@router.get("/{video_id}", response_model=VideoDetailResponse)
async def get_video(video_id: uuid.UUID, session: SessionDep) -> VideoDetailResponse:
    video = await VideoRepository(session).get(video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="video not found")

    analysis_status = "uploaded"
    analysis = None
    if video.latest_analysis_job_id is not None:
        job = await JobRepository(session).get(video.latest_analysis_job_id)
        if job is not None:
            if job.status == JobStatus.COMPLETED:
                analysis_status = "analyzed"
                result = await ResultRepository(session).get(job.id, _ANALYSIS_STAGE)
                analysis = result.payload if result else None
            elif job.status in _TERMINAL_FAILURE_STATUSES:
                analysis_status = "failed"
            else:
                analysis_status = "analyzing"

    return VideoDetailResponse(
        id=video.id,
        project_id=video.project_id,
        original_filename=video.original_filename,
        name=video.name,
        status=analysis_status,
        analysis=analysis,
        created_at=video.created_at,
    )
