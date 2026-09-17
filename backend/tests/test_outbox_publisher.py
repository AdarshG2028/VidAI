"""Tests for the publish-after-ack guarantee.

Requires Postgres; the fake-producer tests don't need Kafka, the real-Kafka
test does (both skip cleanly if their dependency is unreachable).
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.config import get_settings
from backend.messaging.kafka_producer import build_producer
from backend.messaging.outbox_publisher import OutboxPublisher
from backend.models import OutboxEvent

pytestmark = pytest.mark.asyncio


class _FakeProducer:
    """Stands in for AIOKafkaProducer so failure handling can be tested
    without a real broker."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes, headers: list | None = None
    ) -> None:
        if self.fail:
            raise RuntimeError("simulated broker failure")
        self.sent.append((topic, key, value))


def _event(**overrides) -> OutboxEvent:
    defaults = dict(
        aggregate_type="job",
        aggregate_id=uuid.uuid4(),
        event_type="job.created",
        topic="frame_extraction",
        partition_key=str(uuid.uuid4()),
        payload={"hello": "world"},
    )
    defaults.update(overrides)
    return OutboxEvent(**defaults)


async def _maker_for(session: AsyncSession) -> async_sessionmaker:
    """A sessionmaker whose sessions are bound to this test's connection.

    The publisher opens its own session per poll via `async with maker()`,
    so it needs a maker() that hands back a session bound to the test's
    already-open (savepoint) connection rather than a fresh engine.
    """
    conn = await session.connection()
    return async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


async def test_successful_publish_marks_event_published(session: AsyncSession) -> None:
    event = _event()
    session.add(event)
    await session.commit()

    producer = _FakeProducer()
    publisher = OutboxPublisher(
        await _maker_for(session), producer, poll_interval_seconds=1, batch_size=10, max_publish_attempts=10
    )

    published = await publisher.poll_once()

    assert published == 1
    assert producer.sent == [(event.topic, event.partition_key.encode(), b'{"hello": "world"}')]

    await session.refresh(event)
    assert event.status == "published"
    assert event.published_at is not None


async def test_failed_send_leaves_event_pending(session: AsyncSession) -> None:
    event = _event()
    session.add(event)
    await session.commit()

    producer = _FakeProducer(fail=True)
    publisher = OutboxPublisher(
        await _maker_for(session), producer, poll_interval_seconds=1, batch_size=10, max_publish_attempts=10
    )

    published = await publisher.poll_once()

    assert published == 0
    await session.refresh(event)
    assert event.status == "pending"  # never marked published
    assert event.attempts == 1
    assert "simulated broker failure" in event.last_error


async def test_permanently_failing_event_stops_after_max_attempts(session: AsyncSession) -> None:
    """A poison message (e.g. an invalid topic name) must not retry forever.

    Regression test: this exact scenario — an event whose topic can never
    be published — spun in a tight, unbounded loop in production before
    max_publish_attempts existed, because fetch_unpublished_batch() kept
    returning it every single poll with no cap.
    """
    event = _event()
    session.add(event)
    await session.commit()

    producer = _FakeProducer(fail=True)
    publisher = OutboxPublisher(
        await _maker_for(session), producer, poll_interval_seconds=1, batch_size=10, max_publish_attempts=3
    )

    for _ in range(3):
        await publisher.poll_once()

    await session.refresh(event)
    assert event.attempts == 3
    assert event.status == "failed"  # moved out of PENDING; stops showing up in polls

    # A 4th poll must not touch it — it's no longer "unpublished" for polling
    # purposes, even though it never actually reached Kafka.
    published = await publisher.poll_once()
    assert published == 0
    await session.refresh(event)
    assert event.attempts == 3  # unchanged


async def test_already_published_events_are_not_republished(session: AsyncSession) -> None:
    event = _event()
    session.add(event)
    await session.commit()

    producer = _FakeProducer()
    publisher = OutboxPublisher(
        await _maker_for(session), producer, poll_interval_seconds=1, batch_size=10, max_publish_attempts=10
    )
    await publisher.poll_once()
    assert len(producer.sent) == 1

    await publisher.poll_once()  # second poll should find nothing pending
    assert len(producer.sent) == 1


@pytest.mark.usefixtures("kafka_bootstrap_servers")
async def test_publish_against_real_kafka_is_consumable(session: AsyncSession) -> None:
    from aiokafka import AIOKafkaConsumer

    topic = f"setu.test.{uuid.uuid4()}"
    event = _event(topic=topic)
    session.add(event)
    await session.commit()

    producer = build_producer()
    publisher = OutboxPublisher(
        await _maker_for(session), producer, poll_interval_seconds=1, batch_size=10, max_publish_attempts=10
    )
    await publisher.ensure_started()
    try:
        published = await publisher.poll_once()
        assert published == 1

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=get_settings().kafka_bootstrap_servers,
            auto_offset_reset="earliest",
            group_id=f"test-{uuid.uuid4()}",
        )
        await consumer.start()
        try:
            msg = await asyncio.wait_for(consumer.getone(), timeout=15)
            assert msg.key == event.partition_key.encode()
        finally:
            await consumer.stop()
    finally:
        await publisher.stop()
