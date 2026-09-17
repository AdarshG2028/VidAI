"""ProposalApproval — one member's vote on one proposal (Phase 9a).

One row per member per proposal, so a member may change their mind while
the proposal is still pending: the vote is upserted, not appended. That
is deliberately the opposite of `project_jobs`, where a replayed write
must never overwrite the original owner -- there, the first answer is the
true one; here, the latest is.

Votes are kept after the proposal ends, rather than deleted with it: they
are the record of who agreed to the edit that ran.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base


class ProposalApproval(Base):
    __tablename__ = "proposal_approvals"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("proposals.id", ondelete="CASCADE"), primary_key=True
    )
    # No users table; an asserted X-User-Id, matching every other user
    # column in the schema.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)

    decision: Mapped[str] = mapped_column(sa.String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.CheckConstraint(
            "decision IN ('approve','reject')", name="ck_proposal_approvals_decision"
        ),
    )
