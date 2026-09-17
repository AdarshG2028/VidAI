"""Tests for the atomic write + idempotency guarantee at the core of Phase 1.

Requires Postgres; skips cleanly if it's not reachable (see conftest.py).
"""

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import IdempotencyKey, OutboxEvent
from backend.services.job_submission_service import (
    IdempotencyConflictError,
    JobSubmissionService,
)

pytestmark = pytest.mark.asyncio


def _key() -> str:
    return f"test-{uuid.uuid4()}"


async def test_submit_creates_job_and_outbox_event_atomically(
    session: AsyncSession,
) -> None:
    result = await JobSubmissionService(session).submit(
        idempotency_key=_key(),
        workflow=["frame_extraction", "rendering"],
        payload={"video_url": "s3://bucket/in.mp4"},
    )

    assert result.replayed is False
    assert result.job.status == "pending"

    events = (
        await session.execute(
            sa.select(OutboxEvent).where(OutboxEvent.aggregate_id == result.job.id)
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].topic == "frame_extraction"
    assert events[0].status == "pending"


async def test_same_key_and_body_replays_without_duplicating(
    session: AsyncSession,
) -> None:
    key = _key()
    service = JobSubmissionService(session)

    first = await service.submit(
        idempotency_key=key, workflow=["frame_extraction"], payload={"a": 1}
    )
    second = await service.submit(
        idempotency_key=key, workflow=["frame_extraction"], payload={"a": 1}
    )

    assert first.replayed is False
    assert second.replayed is True
    assert first.job.id == second.job.id

    events = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == first.job.id)
        )
    ).scalar_one()
    assert events == 1  # not duplicated by the replay


async def test_same_key_different_body_is_a_conflict(session: AsyncSession) -> None:
    key = _key()
    service = JobSubmissionService(session)

    await service.submit(
        idempotency_key=key, workflow=["frame_extraction"], payload={"a": 1}
    )

    with pytest.raises(IdempotencyConflictError):
        await service.submit(
            idempotency_key=key, workflow=["frame_extraction"], payload={"a": 2}
        )


async def test_concurrent_submissions_with_same_key_yield_one_job(
    database_url: str,
) -> None:
    """The real race: two requests both see no existing key and both insert.

    Uses two independent connections (not the shared `session` fixture,
    since the race needs two genuinely concurrent transactions) against a
    savepoint so the database is left clean afterwards.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    key = _key()

    async def submit_once() -> uuid.UUID:
        async with engine.connect() as conn:
            await conn.begin()
            maker = async_sessionmaker(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            async with maker() as s:
                result = await JobSubmissionService(s).submit(
                    idempotency_key=key,
                    workflow=["frame_extraction"],
                    payload={"race": True},
                )
            await conn.commit()  # persist for real; cleaned up below
            return result.job.id

    job_id: uuid.UUID | None = None
    try:
        job_ids = await asyncio.gather(submit_once(), submit_once())
        assert job_ids[0] == job_ids[1]
        job_id = job_ids[0]

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.select(sa.func.count()).select_from(IdempotencyKey).where(
                        IdempotencyKey.key == key
                    )
                )
            ).scalar_one()
            assert count == 1
    finally:
        from backend.models import Job

        async with engine.connect() as conn:
            await conn.execute(sa.delete(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
            await conn.execute(sa.delete(IdempotencyKey).where(IdempotencyKey.key == key))
            if job_id is not None:
                await conn.execute(sa.delete(Job).where(Job.id == job_id))
            await conn.commit()
    await engine.dispose()
