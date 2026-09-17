"""Processes one dispatched stage: idempotent Result write + Job status
update, in a single Postgres commit.

The crash-recovery guarantee lives at the boundary between this and the
Kafka consumer harness (workers/runner.py), not inside either one alone:
this commits to Postgres FIRST; the harness commits the Kafka offset only
after this returns. A crash between those two commits means Kafka
redelivers a message whose Result already exists — handled here as a
no-op, not reprocessed and not duplicated.

The outcome returned also drives the harness's retry/DLQ decision: RETRY
means back off and let Kafka redeliver; EXHAUSTED means the retry budget
(Job.attempts vs Job.max_attempts, both persisted — not an in-memory
counter that would reset on a crash) is spent and the harness should route
the message to the dead-letter topic and commit the offset to unblock the
partition.

Both commit sites also expect to lose an occasional race to another
delivery of the same message (redelivered before this one's offset
committed, or two worker replicas both live on the same partition during a
rebalance): IntegrityError from a duplicate Result, or StaleDataError from
a concurrent UPDATE to the same Job row finding it already changed. Either
one means somebody else already recorded this attempt's outcome — this
call's own write is redundant, not wrong, so both are caught and treated
the same way IntegrityError always was: log, roll back, return normally.
The alternative — an uncaught StaleDataError killing the worker process
outright — is exactly what took video_analysis down for three days
straight in this project's history; nothing restarted it, and its jobs
sat unprocessed the whole time.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from backend.models import Job, JobStatus, Result, WorkerExecution
from backend.models.enums import ExecutionStatus
from backend.observability.metrics import (
    JOBS_DEAD_LETTERED_TOTAL,
    STAGE_OUTCOMES_TOTAL,
    STAGE_PROCESSING_DURATION_SECONDS,
)
from backend.repositories.job_repository import JobRepository
from backend.repositories.outbox_repository import OutboxRepository
from backend.repositories.result_repository import ResultRepository
from backend.repositories.worker_execution_repository import WorkerExecutionRepository
from backend.workers.base import PermanentError, StageMessage, Worker
from backend.workflow.engine import WorkflowEngine

logger = logging.getLogger(__name__)

# Phase 5's capabilities return {"assets": [{"kind": ..., "uri": ...}]}
# (backend/workers/media.py). Matched here by literal rather than by
# importing media's Asset/AssetKind: this service is Setu-core and stays
# deliberately ignorant of what any particular worker does, treating the
# payload as opaque apart from this one well-known optional convention.
# A worker that returns no assets -- dummy, video_analysis, and every
# pre-Phase-5 worker -- simply leaves artifact_uri NULL, exactly as before.
_ASSETS_KEY = "assets"
_VIDEO_KIND = "video"


def _primary_video_uri(result_payload: dict) -> str | None:
    """The stage's output video, lifted out of the payload into Result's
    dedicated artifact_uri column.

    That column has existed since the schema was first written ("pointer
    into object storage for large artifacts") but was never populated,
    because until Phase 5 no worker produced an artifact. Filling it in
    completes what the schema already anticipated and makes each stage's
    output directly queryable, rather than reachable only by digging
    through a JSON payload.
    """
    assets = result_payload.get(_ASSETS_KEY)
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if isinstance(asset, dict) and asset.get("kind") == _VIDEO_KIND:
            uri = asset.get("uri")
            return uri if isinstance(uri, str) else None
    return None


class ProcessingOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    ALREADY_DONE = "already_done"  # redelivery of a stage that already has a Result
    RETRY = "retry"  # failed, budget remains — harness should back off and not commit
    EXHAUSTED = "exhausted"  # failed, budget spent (or job missing) — harness should DLQ + commit


@dataclass
class ProcessingResult:
    outcome: ProcessingOutcome
    attempt: int = 0
    max_attempts: int = 0
    error: str | None = None


class StageProcessingService:
    def __init__(self, session: AsyncSession, worker: Worker) -> None:
        self._session = session
        self._worker = worker
        self._jobs = JobRepository(session)
        self._results = ResultRepository(session)
        self._executions = WorkerExecutionRepository(session)
        self._engine = WorkflowEngine(OutboxRepository(session))

    async def handle(self, message: StageMessage) -> ProcessingResult:
        existing = await self._results.get(message.job_id, message.stage)
        if existing is not None:
            logger.info(
                "stage already has a result; redelivery, skipping",
                extra={"job_id": str(message.job_id), "stage": message.stage},
            )
            STAGE_OUTCOMES_TOTAL.labels(outcome=ProcessingOutcome.ALREADY_DONE).inc()
            return ProcessingResult(outcome=ProcessingOutcome.ALREADY_DONE)

        job = await self._jobs.get(message.job_id)
        if job is None:
            # Nothing normal processing can do with this — route it to the
            # DLQ for a human to look at rather than either silently
            # dropping it or retrying forever against a job that will never
            # exist.
            logger.error("job not found", extra={"job_id": str(message.job_id)})
            STAGE_OUTCOMES_TOTAL.labels(outcome=ProcessingOutcome.EXHAUSTED).inc()
            return ProcessingResult(
                outcome=ProcessingOutcome.EXHAUSTED, error=f"job {message.job_id} not found"
            )

        if job.status == JobStatus.PENDING:
            job.status = JobStatus.RUNNING
            job.started_at = dt.datetime.now(dt.UTC)

        # Per-stage, not per-job: an earlier stage's successful attempts
        # must not eat into a later stage's retry budget (see
        # WorkerExecutionRepository.count_for_stage's docstring).
        attempt = await self._executions.count_for_stage(message.job_id, message.stage) + 1

        previous_output = None
        if message.stage > 0:
            previous_result = await self._results.get(message.job_id, message.stage - 1)
            previous_output = previous_result.payload if previous_result else None

        start = time.perf_counter()
        try:
            result_payload = await self._worker.process(message, previous_output)
        except Exception as exc:
            STAGE_PROCESSING_DURATION_SECONDS.observe(time.perf_counter() - start)
            return await self._record_failure(job, message, attempt, exc)

        STAGE_PROCESSING_DURATION_SECONDS.observe(time.perf_counter() - start)
        return await self._record_success(job, message, attempt, result_payload)

    async def _record_failure(
        self, job: Job, message: StageMessage, attempt: int, exc: Exception
    ) -> ProcessingResult:
        error_text = str(exc)[:2000]
        # A PermanentError skips straight to EXHAUSTED regardless of budget
        # remaining: the worker is telling us this exact input will fail
        # identically next time too, so spending the retry backoff on it
        # only delays the DLQ, it never avoids it.
        #
        # Read now, before anything below can lose a race: `job.max_attempts`
        # is only ever set at creation, so this value can't go stale, and
        # capturing it here means the return at the bottom never has to
        # touch `job` again after a possibly-failed flush.
        max_attempts = job.max_attempts
        exhausted = attempt >= max_attempts or isinstance(exc, PermanentError)

        try:
            job.attempts = attempt
            job.last_error = error_text
            self._executions.add(
                WorkerExecution(
                    job_id=job.id,
                    worker_name=self._worker.name,
                    stage=message.stage,
                    attempt=attempt,
                    status=ExecutionStatus.FAILED,
                    error=error_text,
                    finished_at=dt.datetime.now(dt.UTC),
                )
            )

            if exhausted:
                # Never over-write a cancellation (Phase 9b). A user who
                # cancelled a job and then saw it reported `dead_lettered`
                # because its in-flight stage happened to fail its last
                # attempt would be told the system broke, when in fact it
                # did what they asked. The execution row above still
                # records the failure, so nothing is hidden -- only the
                # job's own status, which the user has already decided, is
                # left alone.
                #
                # Read fresh for the same reason as the success path:
                # `job` was loaded before the worker ran. This SELECT also
                # autoflushes the mutations above -- a concurrent delivery
                # that already claimed this row can surface a StaleDataError
                # right here, not only at the explicit commit below, which
                # is why the try wraps this and not just the commit.
                if await self._jobs.current_status(job.id) == JobStatus.CANCELLED:
                    job.status = JobStatus.CANCELLED
                else:
                    job.status = JobStatus.DEAD_LETTERED
                # No dedicated "dead_lettered_at" column; completed_at
                # implies success, so leave it null and rely on the
                # auto-maintained updated_at for when this became terminal.

            await self._session.commit()
        except (IntegrityError, StaleDataError):
            # Another delivery of this same message already recorded this
            # attempt's failure (and possibly advanced the job further,
            # e.g. to dead_lettered), whether via a duplicate Result row or
            # a concurrent update to this same Job row. Their write stands;
            # the outcome computed above still drives this call's own
            # return so the harness's retry/DLQ decision for *this*
            # delivery is unaffected -- worst case a harmless extra
            # redelivery, never a crashed worker.
            #
            # Roll back before touching `job` again: a failed flush leaves
            # the session's transaction deactivated, and merely reading an
            # expired ORM attribute (job.id) would make SQLAlchemy try to
            # refresh it from the DB, raising PendingRollbackError instead
            # of the log line it was meant to be. `message.job_id` is a
            # plain value already in hand, not an ORM attribute, so it is
            # safe to read either way -- used for exactly that reason.
            await self._session.rollback()
            logger.info(
                "stage failure commit lost a race to a concurrent delivery; discarding",
                extra={"job_id": str(message.job_id), "stage": message.stage},
            )

        outcome = ProcessingOutcome.EXHAUSTED if exhausted else ProcessingOutcome.RETRY
        STAGE_OUTCOMES_TOTAL.labels(outcome=outcome).inc()
        if exhausted:
            JOBS_DEAD_LETTERED_TOTAL.inc()
        return ProcessingResult(
            outcome=outcome,
            attempt=attempt,
            max_attempts=max_attempts,
            error=error_text,
        )

    async def _record_success(
        self, job: Job, message: StageMessage, attempt: int, result_payload: dict
    ) -> ProcessingResult:
        try:
            self._results.add(
                Result(
                    job_id=job.id,
                    worker_name=self._worker.name,
                    stage=message.stage,
                    payload=result_payload,
                    artifact_uri=_primary_video_uri(result_payload),
                )
            )
            self._executions.add(
                WorkerExecution(
                    job_id=job.id,
                    worker_name=self._worker.name,
                    stage=message.stage,
                    attempt=attempt,
                    status=ExecutionStatus.SUCCEEDED,
                    finished_at=dt.datetime.now(dt.UTC),
                )
            )
            job.attempts = attempt

            # Read fresh rather than trusting `job.status` (Phase 9b): that
            # object was loaded before the worker ran, which for a render
            # is minutes ago, and a cancel arriving during the work is
            # precisely what this needs to see. This SELECT also
            # autoflushes the mutations above -- a concurrent delivery
            # that already claimed this row can surface a StaleDataError
            # right here, not only at the explicit commit below, which is
            # why the try wraps this and not just the commit.
            cancelled = await self._jobs.current_status(job.id) == JobStatus.CANCELLED

            # WorkflowEngine owns workflow position (current_stage, whether
            # another stage exists, dispatching it) — not job lifecycle.
            # It already added the next stage's OutboxEvent to this same
            # session if progress.is_last_stage is False; committing below
            # covers that dispatch atomically along with this stage's
            # Result.
            progress = self._engine.advance(job, message, cancelled=cancelled)
            if progress.is_last_stage and not cancelled:
                job.status = JobStatus.COMPLETED
                job.completed_at = dt.datetime.now(dt.UTC)
            if cancelled:
                # The in-flight stage's Result above is kept -- the user
                # asked to stop, not to discard work already paid for --
                # but the status they set stands. Writing COMPLETED here
                # would undo a deliberate action just because the last
                # stage happened to land first.
                job.status = JobStatus.CANCELLED

            await self._session.commit()
        except (IntegrityError, StaleDataError):
            # Someone else's delivery of the same message already committed
            # a Result for this (job_id, stage) -- IntegrityError -- or
            # already updated this Job row -- StaleDataError, possibly
            # raised by the autoflush inside the current_status() read
            # above rather than by the commit itself. Either way their
            # write stands; ours is redundant, not wrong.
            #
            # `message.job_id`, not `job.id`: a failed flush leaves the
            # session's transaction deactivated, and reading an ORM
            # attribute off the now-possibly-expired `job` would raise
            # PendingRollbackError instead of logging cleanly.
            await self._session.rollback()
            logger.info(
                "stage success commit lost a race to a concurrent delivery; discarding",
                extra={"job_id": str(message.job_id), "stage": message.stage},
            )
        else:
            logger.info(
                "stage processed successfully",
                extra={
                    "job_id": str(job.id),
                    "stage": message.stage,
                    "attempt": attempt,
                    "job_status": job.status,
                },
            )

        STAGE_OUTCOMES_TOTAL.labels(outcome=ProcessingOutcome.SUCCEEDED).inc()
        return ProcessingResult(outcome=ProcessingOutcome.SUCCEEDED, attempt=attempt)
