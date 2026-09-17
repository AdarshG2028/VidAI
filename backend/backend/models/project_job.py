"""ProjectJob — which room a job belongs to, and who set it running.

A mapping table rather than columns on `jobs`, because `jobs` is Setu's
own generic infrastructure and knows nothing about projects, rooms or
this product at all. Keeping the linkage outside it means the execution
engine stays reusable by anything else built on Setu -- the same reason
`Job.payload` is an opaque dict.

`submitted_by_user_id` **is** job ownership (architecture doc, Changelog
v9). Phase 9b's `POST /jobs/{id}/cancel` authorizes directly against it,
which is why it is captured here at submission time rather than being
invented later by Phase 9a's proposal apparatus -- 9b then depends on
nothing from the approval workflow.

Also what makes a job's artifacts scopeable to a room. Before this there
was no path at all from an artifact back to a project, so
`GET /artifacts?uri=...` had nothing to authorize against.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base


class ProjectJob(Base):
    __tablename__ = "project_jobs"

    # The job is the primary key: a job belongs to exactly one room, and
    # this shape makes "which room is this job in" -- the question the
    # artifact guard and the progress poller both ask -- a primary-key
    # lookup rather than a scan.
    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    # No users table; an asserted X-User-Id (backend/api/deps.py), matching
    # projects.owner_id and project_members.user_id.
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        # "Every job in this room", for the snapshot endpoint and the
        # progress poller.
        sa.Index("ix_project_jobs_project_id", "project_id"),
    )
