"""Request/response schemas for the conversation (messages) endpoints.

The planner response shape here is the long-term one from the architecture
doc, not a Phase-2-only format -- "proposal", not "plan", since later
phases add proposal approval and this way nothing gets renamed.
"""

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class PostMessageRequest(BaseModel):
    # sender_id used to be supplied here. It is now the authenticated
    # caller (X-User-Id header, backend/api/deps.py) -- a client can no
    # longer post as somebody else, and more practically, a dependency
    # cannot read a body field, so membership could never be checked
    # against one. Assistant messages still carry sender_id=None; those
    # are never posted by a client.
    content: str


class PlannerMessage(BaseModel):
    type: Literal["message"]
    text: str


class PlannerProposal(BaseModel):
    type: Literal["proposal"]
    summary: str
    workflow: list[dict[str, Any]]


PlannerResponse = Annotated[PlannerMessage | PlannerProposal, Field(discriminator="type")]


class PostMessageResponse(BaseModel):
    message_id: uuid.UUID
    response: PlannerResponse


class ConfirmProposalResponse(BaseModel):
    job_id: uuid.UUID
    replayed: bool


class MessageResponse(BaseModel):
    id: uuid.UUID
    role: str
    sender_id: uuid.UUID | None
    content: str
    created_at: dt.datetime


class ConversationHistoryResponse(BaseModel):
    messages: list[MessageResponse]
