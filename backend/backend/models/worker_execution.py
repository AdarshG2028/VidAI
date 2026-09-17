"""WorkerExecution — one attempt by one worker at one stage.

This is the audit trail the crash-recovery demo reads from: killing a worker
mid-job leaves a 'started' row with no terminal status, and the retry appears
as a new attempt rather than a duplicated result.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base, TimestampMixin
from backend.models.enums import ExecutionStatus


class WorkerExecution(Base, TimestampMixin):
    __tablename__ = "worker_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )

    worker_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Which process handled it — makes horizontal scaling visible in the data.
    worker_instance: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    stage: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=ExecutionStatus.STARTED
    )

    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # Kafka coordinates of the message that triggered this attempt.
    topic: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    partition: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    offset: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="executions")  # noqa: F821

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('started','succeeded','failed')",
            name="ck_worker_executions_status",
        ),
        sa.UniqueConstraint(
            "job_id", "stage", "attempt", name="uq_worker_executions_attempt"
        ),
        sa.Index("ix_worker_executions_job_id", "job_id"),
    )
