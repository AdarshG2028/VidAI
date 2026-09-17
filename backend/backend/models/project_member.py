"""ProjectMember — who is allowed in a room (Phase 8).

The only new table this phase needs. `projects`, `conversations` and
`videos` are all already project-scoped (Changelog v5), and `Message`
has carried `sender_id` since Phase 2 specifically so this phase would
not have to touch it. What was missing was only ever *who else is
allowed in*.

`owner` is not a separate concept from membership: the owner is a member
whose role happens to be 'owner', inserted alongside the project itself
so a project is never memberless. That keeps every read path a single
membership lookup rather than "member OR owner", which is the shape that
invites one of the two checks to be forgotten.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    # No users table yet -- identity is an asserted X-User-Id header
    # (backend/api/deps.py), so this is a plain UUID with no FK, matching
    # projects.owner_id and messages.sender_id.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)

    # 'owner' | 'member'. A plain string rather than an enum: Phase 9a's
    # approval policies are the thing that will actually need roles to
    # mean something, and inventing a type before then would be guessing
    # at its shape.
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="member")

    joined_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        # The composite PK already prevents duplicates; this index serves
        # the other direction -- "which rooms is this user in", which the
        # room list and the socket registry both need.
        sa.Index("ix_project_members_user_id", "user_id"),
    )
