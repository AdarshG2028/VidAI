"""Data access for OutboxEvent."""

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import OutboxEvent
from backend.models.enums import OutboxStatus


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, event: OutboxEvent) -> None:
        self._session.add(event)

    async def fetch_unpublished_batch(self, limit: int) -> list[OutboxEvent]:
        """Oldest-first, so ordering is preserved within a partition key.

        Hits ix_outbox_events_unpublished (WHERE published_at IS NULL).
        """
        result = await self._session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
            .order_by(OutboxEvent.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_published(self, event_id: uuid.UUID) -> None:
        """Called only after Kafka has acked the send — never before."""
        await self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(status=OutboxStatus.PUBLISHED, published_at=dt.datetime.now(dt.UTC))
        )

    async def mark_failed(self, event_id: uuid.UUID, error: str, *, max_attempts: int) -> None:
        """Increments attempts; moves to FAILED once max_attempts is reached.

        Without this cap, an event that can never succeed (e.g. an invalid
        Kafka topic name) stays PENDING forever — fetch_unpublished_batch()
        keeps returning it every poll, so the publisher retries it in a
        tight, unbounded loop indefinitely.
        """
        new_status = sa.case(
            (OutboxEvent.attempts + 1 >= max_attempts, OutboxStatus.FAILED),
            else_=OutboxStatus.PENDING,
        )
        await self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                attempts=OutboxEvent.attempts + 1,
                last_error=error[:2000],
                status=new_status,
            )
        )
