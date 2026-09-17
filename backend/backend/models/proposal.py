"""Proposal — a planned edit awaiting the room's approval (Phase 9a).

Until now a proposal existed only as an assistant message in the
transcript, and `confirm-proposal` found it by scanning history backwards
for the newest JSON blob with `"type": "proposal"`. That worked for one
user with one pending idea. It cannot express *this specific proposal was
approved by these people and became that job*, which is the whole of this
phase.

**Never mutated after it ends.** A rejected proposal keeps its row: it is
the record of what the room turned down, and the planner reads it back in
the transcript when deciding what to suggest next. A revision is a new
row, not an edit -- so the history of what was considered survives, and
`job_id` always points at the workflow that was actually approved rather
than whatever the text was later changed to.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, JSONType, TimestampMixin


class ProposalStatus:
    """Not a StrEnum, matching backend/models/enums.py's reasoning: stored
    as VARCHAR under a CHECK constraint so adding a status stays a plain
    migration instead of an ALTER TYPE."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"

    ALL = (PENDING, APPROVED, REJECTED, SUBMITTED)
    # Nothing further can happen to a proposal in these states, and no
    # vote may be recorded against one.
    TERMINAL = (REJECTED, SUBMITTED)


class Proposal(Base, TimestampMixin):
    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    # Whoever's conversation turn produced this. Copied into
    # project_jobs.submitted_by_user_id when approval triggers submission,
    # so the job belongs to the person whose idea it was rather than to
    # whichever member happened to cast the deciding vote -- Phase 9b
    # authorizes cancellation against exactly that column.
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)

    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Both nullable: they are new in this phase and the planner may omit
    # them. StaticPlanner -- which the whole test suite runs on -- produces
    # neither, and a live model will sometimes skip them too. A proposal
    # without its rationale is worth less, but it is not invalid.
    reasoning: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    discussion_summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # The proposal's workflow exactly as the planner emitted it, in the
    # external `{"stage", "video_ids", "params"}` shape Proposal.from_dict
    # parses. Stored raw rather than compiled: compilation resolves video
    # handles to storage URIs, and doing that at approval time rather than
    # at proposal time means a video uploaded during the discussion is
    # still resolvable when the room finally agrees.
    workflow: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=ProposalStatus.PENDING
    )
    # Set when approval finally produces a Setu job. Nullable for every
    # other state, and no FK cascade concern: jobs outlive proposals.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','submitted')",
            name="ck_proposals_status",
        ),
        # "This room's proposals, newest first" -- the list endpoint, and
        # the lookup that finds what a preview should render.
        sa.Index("ix_proposals_project_id_created_at", "project_id", "created_at"),
    )
