"""Request/response schemas for the jobs endpoints."""

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field

from backend.models import Job


class JobCreateRequest(BaseModel):
    workflow: list[str] = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    id: uuid.UUID
    status: str
    workflow: list[str]
    current_stage: int
    total_stages: int
    attempts: int
    max_attempts: int
    created_at: dt.datetime

    @classmethod
    def from_model(cls, job: Job) -> "JobResponse":
        workflow = job.workflow["workflow"]
        return cls(
            id=job.id,
            status=job.status,
            workflow=workflow,
            current_stage=job.current_stage,
            total_stages=len(workflow),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            created_at=job.created_at,
        )


class MemoryUpdateResponse(BaseModel):
    """`processed` is False when the call did no work — already processed,
    the job isn't finished, or the conversation revealed nothing durable.
    All are successes, not errors."""

    processed: bool
    updated_fields: list[str]
