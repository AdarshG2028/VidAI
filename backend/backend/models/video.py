"""Video — an uploaded source file, always belonging to exactly one Project.
Metadata from Job #1 (video_analysis) is not duplicated here; it's read back
through latest_analysis_job_id, so this row never needs a second write once
analysis starts (see VideoUploadService for why that ordering still leaves
one narrow, accepted crash window).

project_id is NOT NULL and CASCADEs: an orphan video (no project) is not a
representable state, and deleting a project deletes its videos. No unique
constraint on project_id -- a project may hold multiple videos.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, TimestampMixin


class Video(Base, TimestampMixin):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    # Opaque Storage URI (see backend/storage) — never parsed here.
    storage_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # Optional user-entered display name, distinct from original_filename
    # (the uploaded file's own name, which the user doesn't choose). Null
    # until a caller sets one; nothing falls back to original_filename at
    # the model layer -- callers decide how to display an unnamed video.
    name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    # Job #1 (workflow=["video_analysis"]) for this upload. Its status
    # tells callers whether analysis is still running, and once it's
    # completed, Result(job_id=this, stage=0).payload is the video's
    # metadata — no separate metadata column to keep in sync.
    latest_analysis_job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        sa.Index("ix_videos_latest_analysis_job_id", "latest_analysis_job_id"),
        sa.Index("ix_videos_project_id", "project_id"),
    )
