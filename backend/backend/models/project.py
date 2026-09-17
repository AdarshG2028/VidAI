"""Project — the top-level container conversations and videos belong to.

Introduced now, ahead of collaborative rooms (Phase 8), so Conversation can
be parented to it from the start instead of to Video: multi-member rooms
are just Project gaining more members later, not a re-parenting migration.
For Phase 2 every project has exactly one owner and one video.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, TimestampMixin


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    # No users table yet (auth lands in Phase 8) -- plain UUID, no FK.
    owner_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    # How this room decides whether a proposal may run (Phase 9a). An enum
    # column plus two pure functions in services/approval_policy.py, not a
    # table -- a future policy is a new branch there, never a migration.
    # `team` by default: small teams decide collectively, which is only
    # workable because previews are exempt from approval entirely.
    approval_policy: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="team"
    )

    __table_args__ = (
        sa.CheckConstraint(
            "approval_policy IN ('admin','team')", name="ck_projects_approval_policy"
        ),
    )
