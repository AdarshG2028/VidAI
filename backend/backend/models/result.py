"""Result — output produced by one worker stage of a job."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base, JSONType, TimestampMixin


class Result(Base, TimestampMixin):
    __tablename__ = "results"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )

    worker_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    stage: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    # Pointer into object storage for large artifacts (frames, videos).
    artifact_uri: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="results")  # noqa: F821

    __table_args__ = (
        # A stage produces exactly one result per job; retries overwrite it.
        # This is what makes worker re-delivery idempotent.
        sa.UniqueConstraint("job_id", "stage", name="uq_results_job_stage"),
        sa.Index("ix_results_job_id", "job_id"),
    )
