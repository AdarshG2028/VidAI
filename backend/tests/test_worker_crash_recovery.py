"""Kill-mid-job crash recovery — the core Phase 2 deliverable.

Spawns the worker CLI as a genuinely separate OS process (a hard kill is a
much stronger test than asyncio task cancellation, and is what the harness
actually has to survive), kills it while DummyWorker is deliberately
hanging mid-processing (well before it would write anything to Postgres or
commit a Kafka offset), restarts an identical worker on the same consumer
group, and asserts the job completes exactly once: no lost work (the job
finishes) and no duplicate (exactly one Result, one WorkerExecution row).

Requires its own engine with a real commit — the crash-recovery job must be
visible to a separate OS process, so it can't live inside the `session`
fixture's rolled-back savepoint transaction.
"""

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from aiokafka import AIOKafkaProducer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.config import get_settings
from backend.models import Job, Result, WorkerExecution

pytestmark = pytest.mark.usefixtures("database_url", "kafka_bootstrap_servers")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HANG_SECONDS = 8


def _spawn_worker(topic: str, group_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "backend.workers.cli", topic, "--group-id", group_id],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


async def _create_committed_job(engine, topic: str) -> uuid.UUID:
    """A Job row that's really committed, visible to the worker subprocess."""
    async with engine.connect() as conn:
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with maker() as session:
            job = Job(workflow={"workflow": [topic]}, payload={"_hang_seconds": HANG_SECONDS})
            session.add(job)
            await session.commit()
            job_id = job.id
        await conn.commit()
    return job_id


async def _produce_stage_message(job_id: uuid.UUID, topic: str) -> None:
    """Produces directly to Kafka, bypassing the outbox publisher — this
    test is about worker crash recovery, which the outbox path is already
    covered elsewhere for."""
    producer = AIOKafkaProducer(bootstrap_servers=get_settings().kafka_bootstrap_servers)
    await producer.start()
    try:
        await producer.send_and_wait(
            topic,
            key=str(job_id).encode(),
            value=json.dumps(
                {
                    "job_id": str(job_id),
                    "stage": 0,
                    "workflow": [topic],
                    "payload": {"_hang_seconds": HANG_SECONDS},
                }
            ).encode(),
        )
    finally:
        await producer.stop()


async def _row_count(engine, model, job_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.job_id == job_id)
        )


async def _cleanup(engine, job_id: uuid.UUID) -> None:
    async with engine.connect() as conn:
        await conn.execute(sa.delete(WorkerExecution).where(WorkerExecution.job_id == job_id))
        await conn.execute(sa.delete(Result).where(Result.job_id == job_id))
        await conn.execute(sa.delete(Job).where(Job.id == job_id))
        await conn.commit()


@pytest.mark.asyncio
async def test_kill_worker_mid_job_causes_no_loss_and_no_duplication(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    topic = f"crash-recovery-{uuid.uuid4()}"
    group_id = f"setu-{topic}-workers"

    job_id = await _create_committed_job(engine, topic)
    await _produce_stage_message(job_id, topic)

    first_run = _spawn_worker(topic, group_id)
    try:
        # Let it join the consumer group, fetch the message, and enter the
        # hang (HANG_SECONDS=8) — 3s is comfortably inside that window.
        await asyncio.sleep(3)

        assert first_run.poll() is None, "worker exited on its own before being killed"
        first_run.kill()  # hard kill: TerminateProcess on Windows, SIGKILL on POSIX
        first_run.wait(timeout=10)

        # Nothing should have been written: the kill landed inside
        # worker.process(), before any DB write or offset commit exists.
        assert await _row_count(engine, Result, job_id) == 0
        assert await _row_count(engine, WorkerExecution, job_id) == 0
        async with engine.connect() as conn:
            status = await conn.scalar(sa.select(Job.status).where(Job.id == job_id))
        assert status == "pending"

        # Same group_id -> Kafka resumes from the last committed offset,
        # which is still before this message, so it gets redelivered.
        second_run = _spawn_worker(topic, group_id)
        try:
            result_count = 0
            for _ in range(HANG_SECONDS + 20):
                result_count = await _row_count(engine, Result, job_id)
                if result_count >= 1:
                    break
                await asyncio.sleep(1)
        finally:
            second_run.kill()
            second_run.wait(timeout=10)

        assert result_count == 1, "job was not recovered after the worker restarted"
        assert await _row_count(engine, WorkerExecution, job_id) == 1, (
            "expected exactly one execution attempt to have run to completion "
            "(the killed attempt should have written nothing)"
        )
        async with engine.connect() as conn:
            job_status, attempts = (
                await conn.execute(
                    sa.select(Job.status, Job.attempts).where(Job.id == job_id)
                )
            ).one()
        assert job_status == "completed"
        assert attempts == 1
    finally:
        if first_run.poll() is None:
            first_run.kill()
        await _cleanup(engine, job_id)
        await engine.dispose()
