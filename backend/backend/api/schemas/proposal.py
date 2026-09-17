"""Response schemas for proposals and approval progress (Phase 9a)."""

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel

from backend.models import Proposal


class ApprovalProgressResponse(BaseModel):
    """Where a proposal stands, in the terms the room's policy uses.

    `required` is what the policy needs, not a raw member count: under
    `admin` one named person decides, so reporting "1 of 3 approved" would
    describe a vote that is not being held. A client renders this without
    knowing how any policy works.
    """

    policy: str
    approved_by: list[uuid.UUID]
    rejected_by: list[uuid.UUID]
    # Who still has to act for this to pass. Empty under `admin` once the
    # approver has voted, and empty for a proposal that has already ended.
    awaiting: list[uuid.UUID]
    required: int


class ProposalResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    created_by_user_id: uuid.UUID
    summary: str
    reasoning: str | None
    discussion_summary: str | None
    workflow: list[dict[str, Any]]
    status: str
    job_id: uuid.UUID | None
    created_at: dt.datetime

    @classmethod
    def from_model(cls, proposal: Proposal) -> "ProposalResponse":
        return cls(
            id=proposal.id,
            project_id=proposal.project_id,
            created_by_user_id=proposal.created_by_user_id,
            summary=proposal.summary,
            reasoning=proposal.reasoning,
            discussion_summary=proposal.discussion_summary,
            workflow=proposal.workflow.get("workflow", []),
            status=proposal.status,
            job_id=proposal.job_id,
            created_at=proposal.created_at,
        )


class ProposalDetailResponse(ProposalResponse):
    approval: ApprovalProgressResponse


class ProposalListResponse(BaseModel):
    proposals: list[ProposalResponse]


class VoteResponse(BaseModel):
    proposal: ProposalDetailResponse
    # Set only when this vote was the one that satisfied the policy. A
    # client can start following the job immediately rather than
    # discovering it on the next snapshot.
    job_id: uuid.UUID | None = None
