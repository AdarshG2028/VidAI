"""Collecting votes and acting on them (Phase 9a).

The collaboration layer. It decides *whether* a proposal may run; it never
decides what the proposal should be -- that is the planner's job, and the
planner in turn never decides outcomes. Neither half can do the other's
work, which is the property that keeps consensus logic out of code and
policy arithmetic out of the prompt.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Decision, Proposal, ProposalStatus
from backend.repositories.project_member_repository import ProjectMemberRepository
from backend.repositories.project_repository import ProjectRepository
from backend.repositories.proposal_repository import ProposalRepository
from backend.services.approval_policy import ApprovalPolicy, is_rejected, is_satisfied
from backend.services.proposal_confirmation_service import ProposalConfirmationService
from backend.services.room_events import get_room_events

__all__ = ["ProposalClosedError", "ProposalService", "VoteOutcome"]


class ProposalClosedError(Exception):
    """A vote arrived for a proposal that has already ended."""

    def __init__(self, proposal_id: uuid.UUID, status: str) -> None:
        super().__init__(f"proposal {proposal_id} is {status}")
        self.proposal_id = proposal_id
        self.status = status


@dataclass(frozen=True)
class VoteOutcome:
    proposal: Proposal
    # The job an approval produced, when this vote was the one that
    # satisfied the policy. None for every other vote, including the ones
    # that merely bring the room closer.
    job_id: uuid.UUID | None = None


class ProposalService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._proposals = ProposalRepository(session)
        self._members = ProjectMemberRepository(session)
        self._projects = ProjectRepository(session)

    async def vote(
        self, proposal: Proposal, *, user_id: uuid.UUID, decision: Decision
    ) -> VoteOutcome:
        """Record one member's decision and act on it if it settles things.

        A member may change their mind while the proposal is open, so the
        vote is an upsert and the policy is re-evaluated from the full set
        of current votes rather than from a running tally -- a tally would
        have to be un-counted when someone switched.
        """
        if proposal.status in ProposalStatus.TERMINAL:
            raise ProposalClosedError(proposal.id, proposal.status)

        await self._proposals.vote(proposal.id, user_id, decision)
        await self._session.commit()

        project = await self._projects.get(proposal.project_id)
        policy = ApprovalPolicy(project.approval_policy)
        # Only current members count, on both sides: an outstanding
        # invitation must not make unanimity unreachable, and someone who
        # has left must not hold a decision hostage.
        eligible = await self._members.active_user_ids(proposal.project_id)
        votes = {
            voter: vote
            for voter, vote in (await self._proposals.votes(proposal.id)).items()
            if voter in eligible
        }
        arguments = (policy, len(eligible), votes)
        admin = project.owner_id

        if is_rejected(*arguments, admin_user_id=admin):
            return await self._reject(proposal)
        if is_satisfied(*arguments, admin_user_id=admin):
            return await self._approve_and_submit(proposal)

        # Still open -- the state most proposals spend most of their life
        # in. The room still wants to see the vote land.
        self._publish(proposal, "proposal.updated")
        return VoteOutcome(proposal=proposal)

    async def _reject(self, proposal: Proposal) -> VoteOutcome:
        if await self._proposals.claim(proposal.id, to=ProposalStatus.REJECTED):
            await self._session.commit()
            await self._session.refresh(proposal)
            self._publish(proposal, "proposal.updated")
        return VoteOutcome(proposal=proposal)

    async def _approve_and_submit(self, proposal: Proposal) -> VoteOutcome:
        """Compile and run an approved proposal, exactly once.

        **Three phases, and the middle one commits on its own.** The claim
        is a conditional UPDATE, so of two members approving simultaneously
        exactly one proceeds and the room is never billed twice for the
        same render. Submission then commits its own Job/Outbox/
        IdempotencyKey trio inside JobSubmissionService -- reaching into
        that to make it atomic with this would mean modifying Setu's core,
        which this sub-phase deliberately does not do. So a crash between
        submitting and recording leaves a proposal `approved` with no
        job_id: visibly incomplete rather than silently wrong, and
        recoverable, since the idempotency key is derived from the
        proposal id and a retry replays the same job rather than starting
        a second one.
        """
        if not await self._proposals.claim(proposal.id, to=ProposalStatus.APPROVED):
            # Someone else won the race and is submitting it.
            await self._session.refresh(proposal)
            return VoteOutcome(proposal=proposal)
        await self._session.commit()

        result = await ProposalConfirmationService(self._session).submit(
            proposal,
            # Job ownership goes to the proposal's author, not to whoever
            # happened to cast the deciding vote (roadmap, Phase 9b).
            submitted_by=proposal.created_by_user_id,
            preview=False,
        )
        await self._proposals.record_submission(proposal.id, result.job.id)
        await self._session.commit()
        await self._session.refresh(proposal)

        self._publish(proposal, "proposal.updated")
        return VoteOutcome(proposal=proposal, job_id=result.job.id)

    def _publish(self, proposal: Proposal, event_type: str) -> None:
        get_room_events().publish(
            proposal.project_id, type=event_type, data=proposal_event(proposal)
        )


def proposal_event(proposal: Proposal) -> dict:
    """A proposal as the room socket carries it.

    Same field names the REST endpoints return, so a client updating a
    proposal card it already rendered handles one shape, not two. Votes
    are deliberately not included: they are a separate read whose size
    grows with the room, and a client that needs them fetches the
    proposal.
    """
    return {
        "id": str(proposal.id),
        "project_id": str(proposal.project_id),
        "created_by_user_id": str(proposal.created_by_user_id),
        "summary": proposal.summary,
        "reasoning": proposal.reasoning,
        "discussion_summary": proposal.discussion_summary,
        "workflow": proposal.workflow.get("workflow", []),
        "status": proposal.status,
        "job_id": str(proposal.job_id) if proposal.job_id else None,
        "created_at": proposal.created_at.isoformat(),
    }
