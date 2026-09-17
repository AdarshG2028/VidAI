"""Tests for Phase 4's workflow engine: sequential multi-stage dispatch
through the existing outbox pattern.

Runs a WorkerRunner *and* an OutboxPublisher together against a 2-stage
workflow -- no earlier test exercises both at once, since Phase 1-3 only
ever needed one or the other (WorkerRunner tests bypass the outbox by
producing directly; OutboxPublisher tests use a fake producer). This is
the first place stage-to-stage dispatch is exercised end to end: stage A
is produced directly (matching how a real first stage arrives, via the
outbox, but that path is already covered elsewhere), stage B only ever
reaches Kafka because WorkflowEngine.advance() dispatched it and the
publisher drained that row.
"""

import asyncio
import json
import uuid

import pytest
import sqlalchemy as sa
from aiokafka import AIOKafkaProducer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.config import get_settings
from backend.messaging.kafka_producer import build_producer
from backend.messaging.outbox_publisher import OutboxPublisher
from backend.models import Job, OutboxEvent, Result
from backend.services.stage_processing_service import ProcessingOutcome, StageProcessingService
from backend.workers.base import StageMessage, Worker
from backend.workers.dummy_worker import DummyWorker
from backend.workers.runner import WorkerRunner
from backend.workers.stage_workers import (
    FrameExtractionWorker,
    GroundingDinoWorker,
    ProPainterWorker,
    RenderingWorker,
    Sam2Worker,
    TrackingWorker,
    VisionDetectionWorker,
)

pytestmark = pytest.mark.usefixtures("database_url", "kafka_bootstrap_servers")


async def _create_committed_job(
    engine, workflow: list[str], *, max_attempts: int = 5
) -> uuid.UUID:
    async with engine.connect() as conn:
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with maker() as session:
            job = Job(workflow={"workflow": workflow}, payload={}, max_attempts=max_attempts)
            session.add(job)
            await session.commit()
            job_id = job.id
        await conn.commit()
    return job_id


class _FailOnStage(Worker):
    """Fails deterministically on one specific stage index, regardless of
    payload -- lets a test make an early stage succeed and a later stage
    fail without both sharing the same job-level payload flag."""

    name = "fail-on-stage"

    def __init__(self, failing_stage: int) -> None:
        self._failing_stage = failing_stage

    async def process(self, message: StageMessage, previous_output: dict | None) -> dict:
        if message.stage == self._failing_stage:
            raise RuntimeError("simulated failure")
        return {"stage": message.stage}


async def _produce_stage_a(topic: str, job_id: uuid.UUID, workflow: list[str]) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=get_settings().kafka_bootstrap_servers)
    await producer.start()
    try:
        await producer.send_and_wait(
            topic,
            key=str(job_id).encode("utf-8"),
            value=json.dumps(
                {"job_id": str(job_id), "stage": 0, "workflow": workflow, "payload": {}}
            ).encode("utf-8"),
        )
    finally:
        await producer.stop()


async def _outbox_event_count(engine, job_id: uuid.UUID, topic: str) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(sa.func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == job_id, OutboxEvent.topic == topic)
        )


async def _result_count(engine, job_id: uuid.UUID, stage: int) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(sa.func.count())
            .select_from(Result)
            .where(Result.job_id == job_id, Result.stage == stage)
        )


async def _result_payload(engine, job_id: uuid.UUID, stage: int) -> dict:
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(Result.payload).where(Result.job_id == job_id, Result.stage == stage)
        )


async def _job_status_and_stage(engine, job_id: uuid.UUID):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(Job.status, Job.current_stage).where(Job.id == job_id)
            )
        ).one()


async def _wait_for_outbox_published(engine, publisher: OutboxPublisher, job_id: uuid.UUID, topic: str) -> None:
    """Polls rather than asserting this call's own poll_once() did the
    publishing: in a shared dev database, another OutboxPublisher instance
    (e.g. a locally running API process) can legitimately win the race to
    publish this exact row first -- that's the outbox pattern's normal
    at-least-once behavior working as designed (see
    outbox_publisher.py's module docstring), not something a test should
    assume away."""
    await publisher.ensure_started()
    for _ in range(10):
        await publisher.poll_once()
        async with engine.connect() as conn:
            status = await conn.scalar(
                sa.select(OutboxEvent.status).where(
                    OutboxEvent.aggregate_id == job_id, OutboxEvent.topic == topic
                )
            )
        if status == "published":
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"outbox event for topic {topic!r} never reached 'published'")


async def _cleanup(engine, job_id: uuid.UUID) -> None:
    async with engine.connect() as conn:
        await conn.execute(sa.text("DELETE FROM worker_executions WHERE job_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM results WHERE job_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM outbox_events WHERE aggregate_id = :j"), {"j": job_id})
        await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        await conn.commit()


@pytest.mark.asyncio
async def test_two_stage_workflow_dispatches_automatically(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    stage_a_topic = f"workflow-engine-a-{uuid.uuid4()}"
    stage_b_topic = f"workflow-engine-b-{uuid.uuid4()}"
    workflow = [stage_a_topic, stage_b_topic]

    job_id = await _create_committed_job(engine, workflow)
    await _produce_stage_a(stage_a_topic, job_id, workflow)

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    worker_a = WorkerRunner(
        DummyWorker(),
        sessionmaker,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        topic=stage_a_topic,
        group_id=f"setu-{stage_a_topic}-workers",
    )
    worker_b = WorkerRunner(
        DummyWorker(),
        sessionmaker,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        topic=stage_b_topic,
        group_id=f"setu-{stage_b_topic}-workers",
    )
    publisher = OutboxPublisher(
        sessionmaker,
        build_producer(),
        poll_interval_seconds=1,
        batch_size=10,
        max_publish_attempts=5,
    )

    try:
        # --- Stage A: consumed directly (bypassing the outbox, same as
        # other WorkerRunner tests) since this test is about what happens
        # *after* a stage succeeds, not first-stage delivery. ---
        await worker_a.consume_one()

        status, current_stage = await _job_status_and_stage(engine, job_id)
        assert status == "running", "job must not be COMPLETED after only the first stage"
        assert current_stage == 1
        assert await _result_count(engine, job_id, 0) == 1
        assert await _outbox_event_count(engine, job_id, stage_b_topic) == 1, (
            "stage B must be dispatched exactly once when stage A succeeds"
        )

        # --- Replaying stage A's message must not dispatch stage B a
        # second time. Exercised directly against StageProcessingService
        # (the actual mechanism: the existing-Result short-circuit at the
        # top of handle()) rather than via Kafka redelivery gymnastics --
        # this is precisely the code path that has to hold. ---
        replay_message = StageMessage(job_id=job_id, stage=0, workflow=workflow, payload={})
        async with sessionmaker() as session:
            replay_result = await StageProcessingService(session, DummyWorker()).handle(
                replay_message
            )
        assert replay_result.outcome == ProcessingOutcome.ALREADY_DONE
        assert await _outbox_event_count(engine, job_id, stage_b_topic) == 1, (
            "replaying stage A must not create a second stage-B OutboxEvent"
        )
        assert await _result_count(engine, job_id, 0) == 1, "still exactly one Result for stage 0"

        # --- Drain the outbox so stage B's dispatched event actually
        # reaches Kafka, then consume it. ---
        await _wait_for_outbox_published(engine, publisher, job_id, stage_b_topic)
        await worker_b.consume_one()

        status, current_stage = await _job_status_and_stage(engine, job_id)
        assert status == "completed", "job must be COMPLETED only after the final stage"
        assert current_stage == 2
        assert await _result_count(engine, job_id, 1) == 1
        assert await _result_count(engine, job_id, 0) == 1, "stage 0's Result is unaffected"
    finally:
        await worker_a.stop()
        await worker_b.stop()
        await publisher.stop()
        await _cleanup(engine, job_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_three_stage_workflow_with_named_workers(database_url: str) -> None:
    """Same mechanism as the DummyWorker test above (WorkerRunner +
    OutboxPublisher together, sequential dispatch through the outbox),
    but with the actual Frame Extraction -> Vision Detection -> Rendering
    workers from the Phase 4 milestone -- proves the engine dispatches by
    topic name alone and doesn't care which Worker subclass is on the
    other end.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    topics = [
        f"frame-extraction-{uuid.uuid4()}",
        f"vision-detection-{uuid.uuid4()}",
        f"rendering-{uuid.uuid4()}",
    ]
    workflow = topics

    job_id = await _create_committed_job(engine, workflow)
    await _produce_stage_a(topics[0], job_id, workflow)

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    runners = [
        WorkerRunner(
            worker_cls(),
            sessionmaker,
            bootstrap_servers=get_settings().kafka_bootstrap_servers,
            topic=topic,
            group_id=f"setu-{topic}-workers",
        )
        for worker_cls, topic in zip(
            (FrameExtractionWorker, VisionDetectionWorker, RenderingWorker), topics
        )
    ]
    publisher = OutboxPublisher(
        sessionmaker,
        build_producer(),
        poll_interval_seconds=1,
        batch_size=10,
        max_publish_attempts=5,
    )

    try:
        await runners[0].consume_one()  # frame_extraction
        await _wait_for_outbox_published(engine, publisher, job_id, topics[1])
        await runners[1].consume_one()  # vision_detection
        await _wait_for_outbox_published(engine, publisher, job_id, topics[2])
        await runners[2].consume_one()  # rendering

        status, current_stage = await _job_status_and_stage(engine, job_id)
        assert status == "completed", "job must be COMPLETED only after rendering succeeds"
        assert current_stage == 3
        for stage in range(3):
            assert await _result_count(engine, job_id, stage) == 1
    finally:
        for runner in runners:
            await runner.stop()
        await publisher.stop()
        await _cleanup(engine, job_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_six_stage_workflow_each_stage_consumes_previous_output(
    database_url: str,
) -> None:
    """Phase 5's stretch chain: Frame Extraction -> Grounding DINO -> SAM2 ->
    Tracking -> ProPainter -> Rendering. Asserts each stage's Result is
    actually derived from its predecessor's Result (not just that the job
    reaches COMPLETED), since that consumption chain -- not the fake AI
    values themselves -- is the thing this phase demonstrates.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    stage_names = [
        "frame_extraction",
        "grounding_dino",
        "sam2",
        "tracking",
        "propainter",
        "rendering",
    ]
    topics = [f"{name}-{uuid.uuid4()}" for name in stage_names]
    workflow = topics

    job_id = await _create_committed_job(engine, workflow)
    await _produce_stage_a(topics[0], job_id, workflow)

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    worker_classes = (
        FrameExtractionWorker,
        GroundingDinoWorker,
        Sam2Worker,
        TrackingWorker,
        ProPainterWorker,
        RenderingWorker,
    )
    runners = [
        WorkerRunner(
            worker_cls(),
            sessionmaker,
            bootstrap_servers=get_settings().kafka_bootstrap_servers,
            topic=topic,
            group_id=f"setu-{topic}-workers",
        )
        for worker_cls, topic in zip(worker_classes, topics)
    ]
    publisher = OutboxPublisher(
        sessionmaker,
        build_producer(),
        poll_interval_seconds=1,
        batch_size=10,
        max_publish_attempts=5,
    )

    try:
        for i, runner in enumerate(runners):
            await runner.consume_one()
            if i + 1 < len(topics):
                await _wait_for_outbox_published(engine, publisher, job_id, topics[i + 1])

        status, current_stage = await _job_status_and_stage(engine, job_id)
        assert status == "completed"
        assert current_stage == 6

        frames = await _result_payload(engine, job_id, 0)
        detections = await _result_payload(engine, job_id, 1)
        masks = await _result_payload(engine, job_id, 2)
        tracks = await _result_payload(engine, job_id, 3)
        propainter = await _result_payload(engine, job_id, 4)
        rendering = await _result_payload(engine, job_id, 5)

        assert detections["frame_count"] == frames["frame_count"], (
            "grounding_dino must derive its frame_count from frame_extraction's output"
        )
        assert len(masks["masks"]) == len(detections["detections"]), (
            "sam2 must produce one mask entry per detection frame"
        )
        assert tracks["track_count"] >= 1
        assert set(propainter["removed_track_ids"]) == {
            t["track_id"] for t in tracks["tracks"]
        }, "propainter must act on tracking's actual track_ids"
        assert rendering["based_on_output_reference"] == propainter["output_reference"], (
            "rendering must cite propainter's own output_reference, not a fresh value"
        )
    finally:
        for runner in runners:
            await runner.stop()
        await publisher.stop()
        await _cleanup(engine, job_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_later_stage_retry_budget_is_not_reduced_by_earlier_stage_successes(
    database_url: str,
) -> None:
    """Regression test: Job.attempts used to be a single cumulative counter
    incremented on every successful stage too, so a workflow longer than
    max_attempts stages would arrive at its last stage with the retry
    budget already exhausted -- a first-ever failure on that stage would
    dead-letter immediately, with zero retries. The fix scopes the retry
    budget per-stage (WorkerExecutionRepository.count_for_stage), so stage
    1's failures here must get the full max_attempts budget even though
    stage 0 already succeeded once.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    stage_a_topic = f"retry-budget-a-{uuid.uuid4()}"
    stage_b_topic = f"retry-budget-b-{uuid.uuid4()}"
    workflow = [stage_a_topic, stage_b_topic]
    max_attempts = 2

    job_id = await _create_committed_job(engine, workflow, max_attempts=max_attempts)
    await _produce_stage_a(stage_a_topic, job_id, workflow)

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    worker = _FailOnStage(failing_stage=1)
    runner_a = WorkerRunner(
        worker,
        sessionmaker,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        topic=stage_a_topic,
        group_id=f"setu-{stage_a_topic}-workers",
    )
    runner_b = WorkerRunner(
        worker,
        sessionmaker,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        topic=stage_b_topic,
        group_id=f"setu-{stage_b_topic}-workers",
        retry_base_delay_seconds=0.05,
        retry_max_delay_seconds=0.2,
    )
    publisher = OutboxPublisher(
        sessionmaker,
        build_producer(),
        poll_interval_seconds=1,
        batch_size=10,
        max_publish_attempts=5,
    )

    try:
        await runner_a.consume_one()  # stage 0 succeeds
        status, _ = await _job_status_and_stage(engine, job_id)
        assert status == "running"

        await _wait_for_outbox_published(engine, publisher, job_id, stage_b_topic)

        # First failure on stage 1: with the bug, job.attempts was already 1
        # from stage 0's success, so this alone would trip
        # attempt(2) >= max_attempts(2) and dead-letter immediately.
        await runner_b.consume_one()
        status, _ = await _job_status_and_stage(engine, job_id)
        assert status == "running", (
            "stage 1's first-ever failure must RETRY, not exhaust immediately "
            "just because stage 0 already succeeded once"
        )

        # Second failure on stage 1 spends its (correctly-scoped) budget.
        await runner_b.consume_one()
        status, _ = await _job_status_and_stage(engine, job_id)
        assert status == "dead_lettered"
    finally:
        await runner_a.stop()
        await runner_b.stop()
        await publisher.stop()
        await _cleanup(engine, job_id)
        await engine.dispose()
