"""Job — the unit of work submitted by a client and orchestrated by the engine."""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base, JSONType, TimestampMixin
from backend.models.enums import JobStatus


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4
    )

    status: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default=JobStatus.PENDING, index=True
    )

    # Ordered stage names, e.g. {"workflow": ["frame_extraction", "rendering"]}.
    # The engine iterates this; it never hardcodes a chain.
    workflow: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    # Index into workflow["workflow"] of the stage currently dispatched.
    current_stage: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )

    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=5)

    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Set once this job's intermediate artifacts have been swept
    # (backend/services/artifact_cleanup_service.py). Nothing else reads
    # it; it exists so the sweeper can skip jobs it has already handled
    # rather than rescanning every finished job on every pass.
    artifacts_cleaned_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    results: Mapped[list["Result"]] = relationship(  # noqa: F821
        back_populates="job", cascade="all, delete-orphan"
    )
    executions: Mapped[list["WorkerExecution"]] = relationship(  # noqa: F821
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed',"
            "'dead_lettered','cancelled')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        sa.CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts_positive"),
        sa.CheckConstraint("current_stage >= 0", name="ck_jobs_current_stage"),
        # Supports the "what work is outstanding" scan.
        sa.Index("ix_jobs_status_created_at", "status", "created_at"),
    )
