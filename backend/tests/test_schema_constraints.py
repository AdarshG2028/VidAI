"""Integration tests for the guarantees the schema itself must enforce.

These run against the real Postgres from docker/docker-compose.yml and are
skipped when it is not up. They cover the constraints the reliability story
depends on — if these stop holding, idempotency is silently broken.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Job, JobStatus, OutboxEvent, Result, WorkerExecution

pytestmark = pytest.mark.asyncio


async def _make_job(session: AsyncSession) -> Job:
    job = Job(workflow={"workflow": ["frame_extraction", "rendering"]}, payload={})
    session.add(job)
    await session.flush()
    return job


async def test_job_defaults_are_applied(session: AsyncSession) -> None:
    job = await _make_job(session)
    await session.refresh(job)

    assert job.status == JobStatus.PENDING
    assert job.attempts == 0
    assert job.current_stage == 0
    assert job.created_at is not None


async def test_invalid_status_is_rejected(session: AsyncSession) -> None:
    """The CHECK constraint stands in for a native enum."""
    job = await _make_job(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            sa.update(Job).where(Job.id == job.id).values(status="not_a_status")
        )


async def test_one_result_per_job_stage(session: AsyncSession) -> None:
    """Redelivery must not be able to create a second result for a stage."""
    job = await _make_job(session)
    session.add(Result(job_id=job.id, worker_name="frame_extraction", stage=0))
    await session.flush()

    session.add(Result(job_id=job.id, worker_name="frame_extraction", stage=0))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_worker_execution_attempts_are_unique(session: AsyncSession) -> None:
    job = await _make_job(session)
    session.add(
        WorkerExecution(job_id=job.id, worker_name="w", stage=0, attempt=1)
    )
    await session.flush()

    # A retry is a new attempt number, not a duplicate row.
    session.add(
        WorkerExecution(job_id=job.id, worker_name="w", stage=0, attempt=2)
    )
    await session.flush()

    session.add(
        WorkerExecution(job_id=job.id, worker_name="w", stage=0, attempt=2)
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_results_cascade_on_job_delete(session: AsyncSession) -> None:
    job = await _make_job(session)
    session.add(Result(job_id=job.id, worker_name="w", stage=0))
    await session.flush()

    await session.execute(sa.delete(Job).where(Job.id == job.id))
    await session.flush()

    remaining = await session.scalar(
        sa.select(sa.func.count()).select_from(Result).where(Result.job_id == job.id)
    )
    assert remaining == 0


async def test_unpublished_outbox_partial_index_is_used(session: AsyncSession) -> None:
    """The publisher's poll must hit the partial index, not a seq scan.

    Postgres's planner only prefers an index over a seq scan when scanning
    the whole table would actually cost more — on a near-empty table a seq
    scan legitimately wins regardless of the index. So this seeds a batch of
    already-published rows (excluded by the partial predicate, but bulking
    up the table) to reproduce the real scenario the index exists for:
    many published rows, a few unpublished ones the publisher must find
    cheaply.
    """
    await session.execute(
        sa.text(
            "INSERT INTO outbox_events "
            "(id, aggregate_type, aggregate_id, event_type, topic, "
            " partition_key, payload, status, published_at, attempts, "
            " created_at, updated_at) "
            "SELECT gen_random_uuid(), 'job', gen_random_uuid(), 'job.created', "
            " 'setu.jobs', 'k', '{}'::jsonb, 'published', now(), 0, now(), now() "
            "FROM generate_series(1, 5000)"
        )
    )
    session.add(
        OutboxEvent(
            aggregate_type="job",
            aggregate_id=uuid.uuid4(),
            event_type="job.created",
            topic="setu.jobs",
            partition_key="k",
            payload={},
        )
    )
    await session.flush()
    await session.execute(sa.text("ANALYZE outbox_events"))

    plan = await session.scalar(
        sa.text(
            "EXPLAIN (FORMAT JSON) SELECT * FROM outbox_events "
            "WHERE published_at IS NULL ORDER BY created_at LIMIT 100"
        )
    )
    assert "ix_outbox_events_unpublished" in str(plan), (
        f"partial index not used; plan was: {plan}"
    )
