"""StageProcessingService survives a concurrent-write race on its commit.

Regression coverage for the outage this fixes: an uncaught
sqlalchemy.orm.exc.StaleDataError from two deliveries racing on the same
Job row killed the video_analysis worker process outright, and nothing
restarted it for three days -- every job it should have processed since
then sat at status=pending, attempts=0, never even picked up.

Jobs are seeded directly (a topic nothing subscribes to), not submitted
through Kafka -- same reasoning as test_cancellation.py: a real submission
would let the running dev stack race these assertions.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Job, JobStatus
from backend.services.stage_processing_service import (
    ProcessingOutcome,
    StageProcessingService,
)
from backend.workers.base import StageMessage, Worker

pytestmark = pytest.mark.usefixtures("database_url")

WORKFLOW = ["stage-race-test"]


class _Ok(Worker):
    name = "stage-race-test"

    async def process(self, message: StageMessage, previous_output: dict | None) -> dict:
        return {"processed_by": self.name}


@pytest.fixture
async def engine(database_url: str):
    eng = create_async_engine(database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


async def _seed_job(engine, *, max_attempts: int = 3) -> uuid.UUID:
    job_id = uuid.uuid4()
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sm() as session:
        session.add(
            Job(
                id=job_id,
                status=JobStatus.RUNNING,
                workflow={"workflow": WORKFLOW},
                current_stage=0,
                payload={},
                max_attempts=max_attempts,
            )
        )
        await session.commit()
    return job_id


async def _remove_row_from_under(engine, job_id: uuid.UUID) -> None:
    """Simulates a concurrent delivery of the same message that already
    ran to completion and whose own commit landed first -- reproduced here
    by deleting the row entirely, which triggers the exact same "flush
    expected to update 1 row, 0 were matched" StaleDataError a concurrent
    UPDATE-past-this-one would, without needing two live sessions racing
    for real."""
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})


async def _cleanup(engine, job_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM worker_executions WHERE job_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM results WHERE job_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})


@pytest.mark.asyncio
async def test_record_success_survives_a_stale_data_race(engine) -> None:
    job_id = await _seed_job(engine)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with sm() as session:
            service = StageProcessingService(session, _Ok())
            job = await service._jobs.get(job_id)
            assert job is not None

            await _remove_row_from_under(engine, job_id)

            # Must not raise StaleDataError out of here -- that is exactly
            # what killed the real worker process.
            result = await service._record_success(
                job,
                StageMessage(job_id=job_id, stage=0, workflow=WORKFLOW, payload={}),
                attempt=1,
                result_payload={"processed_by": "stage-race-test"},
            )

        assert result.outcome == ProcessingOutcome.SUCCEEDED
    finally:
        await _cleanup(engine, job_id)


@pytest.mark.asyncio
async def test_record_failure_survives_a_stale_data_race(engine) -> None:
    job_id = await _seed_job(engine, max_attempts=3)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with sm() as session:
            service = StageProcessingService(session, _Ok())
            job = await service._jobs.get(job_id)
            assert job is not None

            await _remove_row_from_under(engine, job_id)

            result = await service._record_failure(
                job,
                StageMessage(job_id=job_id, stage=0, workflow=WORKFLOW, payload={}),
                attempt=1,
                exc=RuntimeError("simulated failure"),
            )

        # attempt 1 of 3: budget remains, so RETRY -- the in-memory
        # computation this call falls back to when its own commit is the
        # one that loses the race, per its docstring.
        assert result.outcome == ProcessingOutcome.RETRY
    finally:
        await _cleanup(engine, job_id)


@pytest.mark.asyncio
async def test_record_failure_exhausted_survives_a_stale_data_race(engine) -> None:
    """The exhausted branch reads current_status() -- a SELECT that
    autoflushes pending changes first -- so a concurrent delivery can
    surface the race there, before the explicit commit is ever reached.
    A narrower fix that only wrapped the commit itself would still crash
    here; this is what proves the wider try actually covers it."""
    job_id = await _seed_job(engine, max_attempts=1)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with sm() as session:
            service = StageProcessingService(session, _Ok())
            job = await service._jobs.get(job_id)
            assert job is not None

            await _remove_row_from_under(engine, job_id)

            result = await service._record_failure(
                job,
                StageMessage(job_id=job_id, stage=0, workflow=WORKFLOW, payload={}),
                attempt=1,
                exc=RuntimeError("simulated failure"),
            )

        assert result.outcome == ProcessingOutcome.EXHAUSTED
    finally:
        await _cleanup(engine, job_id)
