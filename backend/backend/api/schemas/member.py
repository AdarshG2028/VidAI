"""Request/response schemas for room membership (Phase 8)."""

import datetime as dt
import uuid

from pydantic import BaseModel


class AddMemberRequest(BaseModel):
    # The person being added, which is the one place a user id legitimately
    # travels in a body: it identifies a *subject*, not the caller. The
    # caller is always the X-User-Id header.
    user_id: uuid.UUID


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    role: str
    joined_at: dt.datetime


class MemberListResponse(BaseModel):
    members: list[MemberResponse]
