"""Reading proposals and voting on them (Phase 9a).

Flat under `/proposals/{id}` rather than nested beneath the project: a
proposal is addressed by its own id, and the room it belongs to is a
property of the row rather than part of its address. Membership is still
what authorizes every call -- resolved from the proposal, so the caller
never has to pass a project id it already implies.

There is deliberately **no creation endpoint**. Proposals come from the
planner, and only from the planner: allowing a client to post one would
make "the LLM plans, the room decides" a convention rather than a
property of the system.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from backend.api.deps import CurrentUserDep, SessionDep
from backend.api.schemas.proposal import (
    ApprovalProgressResponse,
    ProposalDetailResponse,
    ProposalListResponse,
    ProposalResponse,
    VoteResponse,
)
from backend.models import Decision, Proposal, ProposalStatus
from backend.repositories.project_member_repository import ProjectMemberRepository
from backend.repositories.project_repository import ProjectRepository
from backend.repositories.proposal_repository import ProposalRepository
from backend.services.approval_policy import ApprovalPolicy
from backend.services.proposal_service import ProposalClosedError, ProposalService
from backend.services.workflow_compiler import UnknownVideoHandleError

router = APIRouter(tags=["proposals"])


async def _member_only(session, proposal_id: uuid.UUID, user_id: uuid.UUID) -> Proposal:
    """Load a proposal the caller is allowed to see, or 404.

    A non-member and a proposal that does not exist are indistinguishable,
    for the same reason require_project_member returns 404 rather than 403
    -- otherwise a stranger could probe which proposal ids are real.
    """
    proposal = await ProposalRepository(session).get(proposal_id)
    if proposal is None or not await ProjectMemberRepository(session).is_member(
        proposal.project_id, user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        )
    return proposal


async def _progress(session, proposal: Proposal) -> ApprovalProgressResponse:
    project = await ProjectRepository(session).get(proposal.project_id)
    policy = ApprovalPolicy(project.approval_policy)
    eligible = await ProjectMemberRepository(session).active_user_ids(proposal.project_id)
    votes = {
        voter: decision
        for voter, decision in (await ProposalRepository(session).votes(proposal.id)).items()
        if voter in eligible
    }

    approved = sorted(u for u, d in votes.items() if d == Decision.APPROVE)
    rejected = sorted(u for u, d in votes.items() if d == Decision.REJECT)

    if proposal.status in ProposalStatus.TERMINAL:
        awaiting: list[uuid.UUID] = []
    elif policy == ApprovalPolicy.ADMIN:
        # One named person decides, so "who is still to act" is that person
        # or nobody -- other members' silence is not blocking anything.
        awaiting = [] if project.owner_id in votes else [project.owner_id]
    else:
        awaiting = sorted(eligible - set(votes))

    return ApprovalProgressResponse(
        policy=policy.value,
        approved_by=approved,
        rejected_by=rejected,
        awaiting=awaiting,
        required=1 if policy == ApprovalPolicy.ADMIN else len(eligible),
    )


@router.get("/projects/{project_id}/proposals", response_model=ProposalListResponse)
async def list_proposals(
    project_id: uuid.UUID,
    session: SessionDep,
    user_id: CurrentUserDep,
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Optional filter: pending | approved | rejected | submitted.",
    ),
) -> ProposalListResponse:
    """The room's proposals, newest first.

    Rejected ones are included unless filtered out: the roadmap wants them
    shown inline in the room history rather than hidden, because "we
    already turned that down" is the context a returning member most
    needs.
    """
    if not await ProjectMemberRepository(session).is_member(project_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    if status_filter is not None and status_filter not in ProposalStatus.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown status {status_filter!r}",
        )
    proposals = await ProposalRepository(session).list_by_project(
        project_id, status=status_filter
    )
    return ProposalListResponse(
        proposals=[ProposalResponse.from_model(p) for p in proposals]
    )


@router.get("/proposals/{proposal_id}", response_model=ProposalDetailResponse)
async def get_proposal(
    proposal_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> ProposalDetailResponse:
    """One proposal, including where its approval stands."""
    proposal = await _member_only(session, proposal_id, user_id)
    return ProposalDetailResponse(
        **ProposalResponse.from_model(proposal).model_dump(),
        approval=await _progress(session, proposal),
    )


@router.post("/proposals/{proposal_id}/approve", response_model=VoteResponse)
async def approve_proposal(
    proposal_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> VoteResponse:
    """Vote to run this proposal.

    Whether that *does* anything is the policy's decision, not this
    endpoint's: under `team` the last approval submits the job, under
    `admin` only the approver's does, and any other approval simply moves
    the room closer. A member may change their mind while the proposal is
    still open by calling the other endpoint.
    """
    return await _vote(session, proposal_id, user_id, Decision.APPROVE)


@router.post("/proposals/{proposal_id}/reject", response_model=VoteResponse)
async def reject_proposal(
    proposal_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> VoteResponse:
    """Vote against this proposal.

    Under `team` this ends it immediately -- unanimity is already
    impossible, so waiting for the rest of the room to vote would only
    delay a settled outcome. The room's conversation stays open either
    way, and the planner may propose something new.
    """
    return await _vote(session, proposal_id, user_id, Decision.REJECT)


async def _vote(
    session, proposal_id: uuid.UUID, user_id: uuid.UUID, decision: Decision
) -> VoteResponse:
    proposal = await _member_only(session, proposal_id, user_id)
    try:
        outcome = await ProposalService(session).vote(
            proposal, user_id=user_id, decision=decision
        )
    except ProposalClosedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal is already {exc.status}",
        ) from exc
    except UnknownVideoHandleError as exc:
        # The room approved something referring to a video that is no
        # longer resolvable. A 409 rather than a 500: the request was
        # well-formed, the proposal has simply gone stale.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal references unknown video handle '{exc.video_id}'",
        ) from exc

    return VoteResponse(
        proposal=ProposalDetailResponse(
            **ProposalResponse.from_model(outcome.proposal).model_dump(),
            approval=await _progress(session, outcome.proposal),
        ),
        job_id=outcome.job_id,
    )
