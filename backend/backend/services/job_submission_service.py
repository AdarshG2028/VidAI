"""Atomic job submission: Job + OutboxEvent + IdempotencyKey in one commit.

This is the core guarantee of Phase 1. If any part of the write fails, the
whole submission rolls back — there is never a Job without its OutboxEvent,
and a client retrying the same Idempotency-Key never creates a second job.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import propagate, trace
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import IdempotencyKey, Job, OutboxEvent
from backend.observability.metrics import JOBS_SUBMITTED_TOTAL
from backend.repositories.idempotency_repository import IdempotencyRepository
from backend.repositories.job_repository import JobRepository
from backend.repositories.outbox_repository import OutboxRepository

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class IdempotencyConflictError(Exception):
    """Same Idempotency-Key reused with a different request body."""


def _hash_request(workflow: list[str], payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"workflow": workflow, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class JobSubmissionResult:
    job: Job
    replayed: bool  # True if this Idempotency-Key had already been used


class JobSubmissionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)
        self._outbox = OutboxRepository(session)
        self._idempotency = IdempotencyRepository(session)

    async def submit(
        self,
        *,
        idempotency_key: str,
        workflow: list[str],
        payload: dict[str, Any],
    ) -> JobSubmissionResult:
        with tracer.start_as_current_span("job_submission.submit") as span:
            span.set_attribute("idempotency_key", idempotency_key)
            span.set_attribute("workflow", workflow)

            request_hash = _hash_request(workflow, payload)

            existing = await self._idempotency.get(idempotency_key)
            if existing is not None:
                return await self._handle_existing_key(existing, request_hash)

            job = Job(workflow={"workflow": workflow}, payload=payload)
            self._jobs.add(job)
            await self._session.flush()  # assigns job.id
            span.set_attribute("job_id", str(job.id))

            # Captured now, not left for the publisher to assume: by the
            # time OutboxPublisher.poll_once() picks up this row, it's
            # running in a completely different, later async task with no
            # call-stack connection back to this request. Without storing
            # it here, "outbox.publish" would start a new trace instead of
            # continuing this one.
            trace_context: dict[str, str] = {}
            propagate.inject(trace_context)

            self._outbox.add(
                OutboxEvent(
                    aggregate_type="job",
                    aggregate_id=job.id,
                    event_type="job.created",
                    # First stage's name doubles as its topic — the workflow
                    # engine (Phase 4) owns dispatch, not this service.
                    topic=workflow[0],
                    partition_key=str(job.id),
                    payload={
                        "job_id": str(job.id),
                        "stage": 0,
                        "workflow": workflow,
                        "payload": payload,
                    },
                    trace_context=trace_context,
                )
            )
            self._idempotency.add(
                IdempotencyKey(key=idempotency_key, job_id=job.id, request_hash=request_hash)
            )

            try:
                await self._session.commit()
            except IntegrityError:
                # Lost a concurrent race on the same key — the winner's row is
                # now committed by another request; replay it instead of failing.
                await self._session.rollback()
                existing = await self._idempotency.get(idempotency_key)
                if existing is None:
                    raise  # not a key collision; some other integrity failure
                return await self._handle_existing_key(existing, request_hash)

            logger.info(
                "job created",
                extra={
                    "job_id": str(job.id),
                    "workflow": workflow,
                    "idempotency_key": idempotency_key,
                },
            )
            JOBS_SUBMITTED_TOTAL.inc()
            return JobSubmissionResult(job=job, replayed=False)

    async def _handle_existing_key(
        self, existing: IdempotencyKey, request_hash: str
    ) -> JobSubmissionResult:
        # Runs inside submit()'s span (still current, not a separate one) --
        # replay/conflict are outcomes of the same submission, not a
        # different operation.
        span = trace.get_current_span()
        span.set_attribute("job_id", str(existing.job_id))
        span.set_attribute("replayed", True)
        if existing.request_hash != request_hash:
            logger.warning(
                "idempotency key reused with a different request body",
                extra={"job_id": str(existing.job_id), "idempotency_key": existing.key},
            )
            raise IdempotencyConflictError(
                f"Idempotency-Key {existing.key!r} was already used with a "
                "different request body"
            )
        job = await self._jobs.get(existing.job_id)
        assert job is not None, "idempotency key points at a missing job"
        logger.info(
            "job submission replayed via idempotency key",
            extra={"job_id": str(job.id), "idempotency_key": existing.key},
        )
        return JobSubmissionResult(job=job, replayed=True)
