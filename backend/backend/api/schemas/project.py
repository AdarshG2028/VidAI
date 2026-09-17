"""Request/response schemas for the projects endpoint."""

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel


class CreateProjectRequest(BaseModel):
    # owner_id used to live here. It now comes from the X-User-Id header
    # (backend/api/deps.py): a body field cannot be read by a dependency,
    # so a membership guard could never have checked it.
    name: str | None = None


class UpdateProjectRequest(BaseModel):
    """Room-level settings an owner may change (Phase 9a).

    The only field today is the approval policy -- there was previously
    no way to select `admin` at all, since `team` is the migration
    default and nothing wrote to the column afterward. `Literal` rather
    than a free string: an invalid policy is rejected by validation
    before it reaches the database's own CHECK constraint.
    """

    approval_policy: Literal["admin", "team"] | None = None


class ProjectResponse(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str | None
    approval_policy: str
    created_at: dt.datetime
