"""FastAPI application factory and ASGI entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from backend.api.routes import (
    artifacts,
    health,
    jobs,
    projects,
    proposals,
    room_socket,
    videos,
)
from backend.core.config import Settings, get_settings
from backend.database.session import get_sessionmaker
from backend.messaging.kafka_producer import build_producer
from backend.messaging.outbox_publisher import OutboxPublisher
from backend.observability.logging import configure_logging
from backend.observability.metrics import poll_job_lifecycle_gauges
from backend.services.artifact_cleanup_service import ArtifactCleanupService
from backend.services.job_progress_poller import JobProgressPoller
from backend.services.room_events import get_room_events
from backend.observability.tracing import configure_tracing

logger = logging.getLogger(__name__)


async def _sweep_artifacts_forever(stop_event: asyncio.Event, settings: Settings) -> None:
    """Periodically drop intermediate artifacts from long-finished jobs.

    Runs in the API rather than a worker because it is storage-wide
    housekeeping, not per-stage work, and the API is the one process
    guaranteed to be running. Every failure is swallowed and retried next
    tick: reclaiming disk is never worth taking the API down for.
    """
    if not settings.artifact_cleanup_enabled:
        return
    while not stop_event.is_set():
        try:
            async with get_sessionmaker()() as session:
                await ArtifactCleanupService(session).sweep(
                    retention_hours=settings.artifact_retention_hours
                )
        except Exception:
            logger.exception("artifact cleanup pass failed; will retry")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.artifact_cleanup_interval_seconds
            )
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Starts the outbox publisher and the job-lifecycle metrics poller as
    background tasks.

    The publisher connects to Kafka lazily (see
    OutboxPublisher.ensure_started), so a Kafka outage at boot never blocks
    the API from serving requests — jobs still get written to the outbox
    and drain once the broker is reachable.
    """
    settings = get_settings()
    publisher = OutboxPublisher(
        get_sessionmaker(),
        build_producer(),
        poll_interval_seconds=settings.outbox_poll_interval_seconds,
        batch_size=settings.outbox_batch_size,
        max_publish_attempts=settings.outbox_max_publish_attempts,
        publish_timeout_seconds=settings.outbox_publish_timeout_seconds,
    )
    stop_event = asyncio.Event()
    publisher_task = asyncio.create_task(publisher.run_forever(stop_event))
    cleanup_task = asyncio.create_task(
        _sweep_artifacts_forever(stop_event, settings)
    )
    metrics_task = asyncio.create_task(
        poll_job_lifecycle_gauges(
            get_sessionmaker(), stop_event, settings.metrics_poll_interval_seconds
        )
    )
    # Setu's engine emits nothing when a job advances, so this is the
    # bridge onto the room socket. It costs nothing until somebody
    # connects: with no open sockets it reads no jobs at all.
    progress_task = asyncio.create_task(
        JobProgressPoller(
            get_sessionmaker(),
            get_room_events(),
            interval_seconds=settings.room_progress_poll_interval_seconds,
        ).run_forever(stop_event)
    )

    try:
        yield
    finally:
        stop_event.set()
        # Before awaiting anything: every open room socket is parked on a
        # queue read that nothing will ever satisfy again, and shutdown
        # would wait on those tasks forever.
        get_room_events().close_all()
        await publisher_task
        await metrics_task
        await cleanup_task
        await progress_task
        await publisher.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    if settings.tracing_enabled:
        configure_tracing(settings, service_name="setu-api")

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )
    origins = (
        ["*"]
        if settings.cors_allowed_origins == "*"
        else [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # No cookies/sessions to protect (identity is an asserted header,
        # see backend/api/deps.py) -- allow_credentials stays False so
        # allow_origins=["*"] remains valid per the CORS spec, which
        # forbids combining a wildcard origin with credentialed requests.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(artifacts.router)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(videos.router)
    app.include_router(projects.router)
    app.include_router(proposals.router)
    app.include_router(room_socket.router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
