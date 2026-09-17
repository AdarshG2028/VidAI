"""IdempotencyKey — collapses duplicate job submissions from retrying clients."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, TimestampMixin


class IdempotencyKey(Base, TimestampMixin):
    __tablename__ = "idempotency_keys"

    # The client-supplied key is the primary key: a concurrent duplicate loses
    # the insert race and is served the original job instead.
    key: Mapped[str] = mapped_column(sa.String(255), primary_key=True)

    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    # Hash of the request body: same key + different body is a client bug,
    # and we surface it as 422 rather than silently returning the wrong job.
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    __table_args__ = (sa.Index("ix_idempotency_keys_job_id", "job_id"),)
