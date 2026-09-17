"""Request/response schemas for the videos endpoints."""

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field


class RenameVideoRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class VideoUploadResponse(BaseModel):
    video_id: uuid.UUID
    project_id: uuid.UUID
    job_id: uuid.UUID
    name: str | None
    status: str


class VideoDetailResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    original_filename: str
    name: str | None
    # "uploaded" (no analysis job yet — shouldn't normally be observed,
    # upload always submits one) | "analyzing" | "analyzed" | "failed"
    status: str
    analysis: dict[str, Any] | None
    created_at: dt.datetime


class VideoSummaryResponse(BaseModel):
    """Lighter shape for listing a project's videos -- no per-video
    job/result lookup, unlike VideoDetailResponse. Callers wanting analysis
    status/payload for a specific video use GET /videos/{id}."""

    id: uuid.UUID
    original_filename: str
    name: str | None
    created_at: dt.datetime


class VideoListResponse(BaseModel):
    videos: list[VideoSummaryResponse]
