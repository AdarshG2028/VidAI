"""Tests for exponential backoff and dead-letter queue handling.

Uses WorkerRunner.consume_one() directly (in-process, real Kafka + Postgres)
rather than spawning a subprocess like the crash-recovery test — nothing
here needs killing, just observing retry/backoff/DLQ behavior across
repeated deliveries of the same message.
"""

import asyncio
import json
import uuid

import pytest
import sqlalchemy as sa
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.config import get_settings
from backend.models import Job
from backend.workers.base import PermanentError, StageMessage, Worker
from backend.workers.dummy_worker import DummyWorker
from backend.workers.runner import WorkerRunner, retry_delay_seconds

pytestmark = pytest.mark.usefixtures("database_url", "kafka_bootstrap_servers")


class _AlwaysPermanentlyFailingWorker(Worker):
    """Simulates a worker whose input can never succeed (e.g. an
    unsupported uploaded file) -- every attempt raises PermanentError, so
    a test can confirm the harness DLQs on attempt 1 instead of spending
    the full retry budget's backoff on a foregone conclusion."""

    name = "always-permanently-failing"

    async def process(self, message: StageMessage, previous_output: dict | None) -> dict:
        raise PermanentError("simulated permanent failure")


async def _create_committed_job(
    engine, topic: str, *, max_attempts: int, fail: bool
) -> uuid.UUID:
    async with engine.connect() as conn:
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with maker() as session:
            job = Job(
                workflow={"workflow": [topic]},
                payload={"_fail": fail},
                max_attempts=max_attempts,
            )
            session.add(job)
            await session.commit()
            job_id = job.id
        await conn.commit()
    return job_id


async def _produce(topic: str, job_id: uuid.UUID, *, fail: bool) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=get_settings().kafka_bootstrap_servers)
    await producer.start()
    try:
        await producer.send_and_wait(
            topic,
            key=str(job_id).encode("utf-8"),
            value=json.dumps(
                {
                    "job_id": str(job_id),
                    "stage": 0,
                    "workflow": [topic],
                    "payload": {"_fail": fail},
                }
            ).encode("utf-8"),
        )
    finally:
        await producer.stop()


async def _cleanup(engine, job_id: uuid.UUID) -> None:
    async with engine.connect() as conn:
        await conn.execute(sa.text("DELETE FROM worker_executions WHERE job_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM results WHERE job_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        await conn.commit()


@pytest.mark.asyncio
async def test_failing_job_retries_then_dead_letters(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    topic = f"retry-dlq-{uuid.uuid4()}"
    max_attempts = 3
    job_id = await _create_committed_job(engine, topic, max_attempts=max_attempts, fail=True)
    await _produce(topic, job_id, fail=True)

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    runner = WorkerRunner(
        DummyWorker(),
        sessionmaker,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        topic=topic,
        group_id=f"setu-{topic}-workers",
        retry_base_delay_seconds=0.05,
        retry_max_delay_seconds=0.2,
    )

    job_id_2 = None
    try:
        # max_attempts=3: attempts 1 and 2 -> RETRY (tiny backoff, message
        # redelivered since the offset isn't committed); attempt 3 ->
        # EXHAUSTED -> DLQ + offset committed.
        for _ in range(max_attempts):
            await runner.consume_one()

        async with engine.connect() as conn:
            status, attempts = (
                await conn.execute(sa.select(Job.status, Job.attempts).where(Job.id == job_id))
            ).one()
        assert status == "dead_lettered"
        assert attempts == max_attempts

        dlq_consumer = AIOKafkaConsumer(
            f"{topic}.dlq",
            bootstrap_servers=get_settings().kafka_bootstrap_servers,
            auto_offset_reset="earliest",
            group_id=f"test-dlq-{uuid.uuid4()}",
        )
        await dlq_consumer.start()
        try:
            msg = await asyncio.wait_for(dlq_consumer.getone(), timeout=10)
            dlq_payload = json.loads(msg.value)
            assert dlq_payload["job_id"] == str(job_id)
            assert dlq_payload["error"]
        finally:
            await dlq_consumer.stop()

        # The partition must be unblocked: a fresh, healthy message on the
        # SAME topic should be picked up promptly, not stuck forever behind
        # the poison one.
        job_id_2 = await _create_committed_job(engine, topic, max_attempts=5, fail=False)
        await _produce(topic, job_id_2, fail=False)
        await runner.consume_one()

        async with engine.connect() as conn:
            status_2 = await conn.scalar(sa.select(Job.status).where(Job.id == job_id_2))
        assert status_2 == "completed"
    finally:
        await runner.stop()
        await _cleanup(engine, job_id)
        if job_id_2 is not None:
            await _cleanup(engine, job_id_2)
        await engine.dispose()


@pytest.mark.asyncio
async def test_permanent_error_dead_letters_on_first_attempt(database_url: str) -> None:
    """A worker raising PermanentError should skip retries entirely -- DLQ
    on attempt 1 even though max_attempts leaves budget remaining -- since
    the same input is guaranteed to fail identically on every redelivery."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    topic = f"permanent-error-{uuid.uuid4()}"
    max_attempts = 5
    job_id = await _create_committed_job(engine, topic, max_attempts=max_attempts, fail=True)
    await _produce(topic, job_id, fail=True)

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    runner = WorkerRunner(
        _AlwaysPermanentlyFailingWorker(),
        sessionmaker,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        topic=topic,
        group_id=f"setu-{topic}-workers",
        retry_base_delay_seconds=0.05,
        retry_max_delay_seconds=0.2,
    )

    try:
        await runner.consume_one()  # single delivery should be enough to DLQ

        async with engine.connect() as conn:
            status, attempts = (
                await conn.execute(sa.select(Job.status, Job.attempts).where(Job.id == job_id))
            ).one()
        assert status == "dead_lettered"
        assert attempts == 1  # never touched attempts 2-5 of its budget
    finally:
        await runner.stop()
        await _cleanup(engine, job_id)
        await engine.dispose()


def test_retry_backoff_doubles_each_attempt() -> None:
    """The backoff progression, asserted on the calculation itself.

    This used to be an integration test measuring wall-clock time between
    consume_one() calls. It was intermittently failing: the first call
    also pays Kafka consumer-group join and metadata-fetch cost, so under
    load the first delta could exceed the second and the assertion flipped
    even though the backoff was perfectly correct. It was measuring the
    harness, not the thing under test.
    """
    delays = [retry_delay_seconds(attempt, base=0.3, maximum=5.0) for attempt in (1, 2, 3)]

    assert delays == [0.3, 0.6, 1.2]


def test_retry_backoff_is_capped() -> None:
    """The cap is what stops a long retry budget turning into an
    unbounded wait — worth pinning separately from the doubling."""
    assert retry_delay_seconds(attempt=10, base=2.0, maximum=30.0) == 30.0
    assert retry_delay_seconds(attempt=1, base=40.0, maximum=30.0) == 30.0


def test_retry_backoff_starts_at_base_on_the_first_attempt() -> None:
    """attempt is 1-based, matching StageProcessingService's counter. An
    off-by-one here would either skip the first backoff entirely or double
    every wait in production."""
    assert retry_delay_seconds(attempt=1, base=2.0, maximum=30.0) == 2.0


@pytest.mark.asyncio
async def test_a_slow_stage_does_not_get_the_consumer_evicted(monkeypatch) -> None:
    """aiokafka assumes a consumer has died if it does not poll within
    max_poll_interval_ms, which defaults to 300 seconds -- and this
    harness awaits the ENTIRE stage between polls, so that default is
    really a five-minute cap on how long any one stage may take.

    Exceeding it does not raise. The consumer is evicted, its offset never
    commits, the partition is reassigned and the message is redelivered --
    so the same work runs again. For find_content that means re-billing
    every frame of a paid vision API, in a loop, while the logs show
    nothing worse than a rebalance.

    find_content is paced by the provider's tokens-per-minute budget and
    takes minutes by design; long renders and merges can too. So the
    interval is configured explicitly rather than defaulted, and this test
    is here because the failure is invisible until it costs money.
    """
    captured: dict = {}

    class _FakeConsumer:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def start(self):
            return None

    monkeypatch.setattr("backend.workers.runner.AIOKafkaConsumer", _FakeConsumer)
    monkeypatch.setattr(
        "backend.workers.runner.AIOKafkaProducer",
        type("_P", (), {"__init__": lambda self, *a, **k: None, "start": lambda self: _noop()}),
    )

    runner = WorkerRunner(
        DummyWorker(),
        None,
        bootstrap_servers="localhost:19092",
        topic="whatever",
        group_id="whatever-group",
        max_poll_interval_seconds=1800.0,
    )
    await runner._ensure_started()

    assert captured["max_poll_interval_ms"] == 1_800_000, (
        "the runner must pass the configured interval through to aiokafka, "
        f"got {captured.get('max_poll_interval_ms')}"
    )


async def _noop():
    return None


def test_the_default_poll_interval_exceeds_a_realistic_slow_stage() -> None:
    """A guard on the value, not just the wiring. A find_content search on
    the free tier is paced to under three frames a minute, so a 150-frame
    search runs for the better part of an hour -- anything near aiokafka's
    own 300s default silently reintroduces the duplication."""
    assert get_settings().worker_max_poll_interval_seconds >= 1800.0
