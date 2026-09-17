"""Data access for Proposal and ProposalApproval. No policy logic — that
lives in services/approval_policy.py."""

import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Decision, Proposal, ProposalApproval, ProposalStatus


class ProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, proposal: Proposal) -> None:
        self._session.add(proposal)

    async def get(self, proposal_id: uuid.UUID) -> Proposal | None:
        result = await self._session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        )
        return result.scalar_one_or_none()

    async def list_by_project(
        self, project_id: uuid.UUID, *, status: str | None = None
    ) -> list[Proposal]:
        """The room's proposals, newest first.

        Rejected ones are included by default: the roadmap asks for them
        shown inline in the room history rather than hidden, because "we
        already turned that down" is the context a member returning to the
        room most needs.
        """
        query = select(Proposal).where(Proposal.project_id == project_id)
        if status is not None:
            query = query.where(Proposal.status == status)
        result = await self._session.execute(
            query.order_by(Proposal.created_at.desc(), Proposal.id)
        )
        return list(result.scalars().all())

    async def latest_pending(self, project_id: uuid.UUID) -> Proposal | None:
        """What a preview renders, and what a bare approve applies to."""
        result = await self._session.execute(
            select(Proposal)
            .where(
                Proposal.project_id == project_id,
                Proposal.status == ProposalStatus.PENDING,
            )
            .order_by(Proposal.created_at.desc(), Proposal.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def claim(self, proposal_id: uuid.UUID, *, to: str) -> bool:
        """Move a *pending* proposal to `to`, and report whether we won.

        A conditional UPDATE rather than read-then-write, and the reason
        this is a repository method at all. Two members approving at the
        same instant both see a satisfied policy; without the status in
        the WHERE clause both would then compile and submit, and the room
        would pay for the same render twice. Exactly one caller gets
        rowcount 1.
        """
        result = await self._session.execute(
            update(Proposal)
            .where(Proposal.id == proposal_id, Proposal.status == ProposalStatus.PENDING)
            .values(status=to)
        )
        return result.rowcount == 1

    async def record_submission(self, proposal_id: uuid.UUID, job_id: uuid.UUID) -> None:
        """Attach the job an approved proposal produced."""
        await self._session.execute(
            update(Proposal)
            .where(Proposal.id == proposal_id)
            .values(status=ProposalStatus.SUBMITTED, job_id=job_id)
        )

    async def vote(
        self, proposal_id: uuid.UUID, user_id: uuid.UUID, decision: Decision
    ) -> None:
        """Record a member's decision, replacing any earlier one.

        Upsert rather than insert-if-absent: a member may change their
        mind while the proposal is still open. Deliberately the opposite
        of ProjectJobRepository.add, where a replayed write must never
        overwrite the first answer -- there the original is the true one,
        here the latest is.
        """
        await self._session.execute(
            insert(ProposalApproval)
            .values(proposal_id=proposal_id, user_id=user_id, decision=decision.value)
            .on_conflict_do_update(
                index_elements=["proposal_id", "user_id"],
                set_={"decision": decision.value},
            )
        )

    async def votes(self, proposal_id: uuid.UUID) -> dict[uuid.UUID, Decision]:
        result = await self._session.execute(
            select(ProposalApproval.user_id, ProposalApproval.decision).where(
                ProposalApproval.proposal_id == proposal_id
            )
        )
        return {user_id: Decision(decision) for user_id, decision in result.all()}
