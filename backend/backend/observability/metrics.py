"""Prometheus metrics shared across Setu processes.

Two separate scrape targets exist because the API and each worker are
independent OS processes with independent in-memory registries: the API
exposes /metrics (mounted in backend/api/main.py) and each worker runs its
own metrics HTTP server on --metrics-port (backend/workers/cli.py). A
worker's counters aren't visible to the API and vice versa — Prometheus
scrapes both and Grafana merges them into one dashboard.
"""

import asyncio
import logging

import sqlalchemy as sa
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.models import Job
from backend.models.enums import JobStatus

logger = logging.getLogger(__name__)

# --- API: job submission ---
JOBS_SUBMITTED_TOTAL = Counter(
    "setu_jobs_submitted_total",
    "Jobs successfully submitted as new jobs (idempotent replays don't count).",
)

# --- API: outbox publisher (runs as a background task inside the API process) ---
OUTBOX_PUBLISH_TOTAL = Counter(
    "setu_outbox_publish_total",
    "Outbox publish attempts, by outcome.",
    ["outcome"],  # published | failed
)

# --- Worker: stage processing ---
STAGE_PROCESSING_DURATION_SECONDS = Histogram(
    "setu_stage_processing_duration_seconds",
    "Time spent in Worker.process() for one stage attempt (success or failure).",
)
STAGE_OUTCOMES_TOTAL = Counter(
    "setu_stage_outcomes_total",
    "Stage processing outcomes.",
    ["outcome"],  # succeeded | already_done | retry | exhausted
)
JOBS_DEAD_LETTERED_TOTAL = Counter(
    "setu_jobs_dead_lettered_total",
    "Jobs that exhausted their retry budget and were dead-lettered.",
)

# --- Job lifecycle: current count of jobs in each status. These are
# Gauges refreshed periodically from Postgres (the source of truth), not
# counters incremented at transition points -- "pending" and "processing"
# must be able to go down as well as up, which a plain Counter can't
# express. Only the API polls (see poll_job_lifecycle_gauges below); the
# Jobs table is one shared source of truth, so workers don't duplicate it.
JOBS_PENDING = Gauge("setu_jobs_pending", "Jobs currently in PENDING status.")
JOBS_PROCESSING = Gauge("setu_jobs_processing", "Jobs currently in RUNNING status.")
JOBS_COMPLETED = Gauge("setu_jobs_completed", "Jobs currently in COMPLETED status.")
JOBS_FAILED = Gauge(
    "setu_jobs_failed",
    "Jobs currently in DEAD_LETTERED status -- the terminal failure state this "
    "pipeline actually produces (JobStatus.FAILED exists but nothing sets it today).",
)

_STATUS_GAUGES: dict[JobStatus, Gauge] = {
    JobStatus.PENDING: JOBS_PENDING,
    JobStatus.RUNNING: JOBS_PROCESSING,
    JobStatus.COMPLETED: JOBS_COMPLETED,
    JobStatus.DEAD_LETTERED: JOBS_FAILED,
}


async def poll_job_lifecycle_gauges(
    sessionmaker: async_sessionmaker, stop_event: asyncio.Event, interval_seconds: float
) -> None:
    """Keeps the JOBS_* gauges in sync with Postgres. Runs as a background
    task in the API process (see backend/api/main.py's lifespan), started
    and stopped the same way OutboxPublisher.run_forever() already is."""
    while not stop_event.is_set():
        try:
            async with sessionmaker() as session:
                rows = (
                    await session.execute(
                        sa.select(Job.status, sa.func.count()).group_by(Job.status)
                    )
                ).all()
            counts = dict.fromkeys(_STATUS_GAUGES, 0)
            for status, count in rows:
                if status in counts:
                    counts[status] = count
            for status, gauge in _STATUS_GAUGES.items():
                gauge.set(counts[status])
        except Exception:
            logger.exception("job lifecycle gauge poll failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
