"""Generic Kafka consumer harness for any Worker implementation.

Per-message flow: consume -> StageProcessingService.handle() (idempotent
Postgres commit) -> commit the Kafka offset, strictly in that order. This
ordering is the entire crash-recovery guarantee:
  - Crash before the Postgres commit: nothing was persisted, offset was
    never committed, Kafka redelivers from the same point on restart.
  - Crash after the Postgres commit but before the offset commit: Kafka
    redelivers the same message, but StageProcessingService sees the
    Result already exists and treats it as a no-op.
Either way: no lost work, no duplicated Result.

enable_auto_commit=False is what makes this true — auto-commit would ack
the offset on a timer regardless of whether the message was actually
processed, which can lose work on a crash.

On failure, the outcome from StageProcessingService decides what happens:
  - RETRY (budget remains): sleep for an exponentially increasing delay,
    then explicitly seek() the consumer back to the failed record's offset
    before returning without committing. This is not optional bookkeeping:
    within one live consumer instance the fetch position auto-advances on
    every getone() regardless of whether the offset was committed — an
    uncommitted offset only causes redelivery across a consumer restart
    (that's what the crash-recovery path relies on). Without the seek,
    run_forever()'s single long-lived consumer would silently skip a
    failed message instead of retrying it. This blocks the partition for
    the sleep duration — accepted for this single-worker demo; a
    production system would isolate slow/failing jobs onto their own
    partition or a delay queue instead. Cumulative backoff is capped well
    under Kafka's max.poll.interval.ms (~5 min default) so the broker
    doesn't rebalance the partition away mid-retry.
  - EXHAUSTED (budget spent, or the job no longer exists): publish the
    message to "<topic>.dlq" and commit the offset, unblocking the
    partition. Direct produce, not routed through the outbox — this is a
    worker-internal failure record, not a domain event the rest of the
    system needs guaranteed delivery of.
"""

import asyncio
import json
import logging
import uuid

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.services.stage_processing_service import ProcessingOutcome, StageProcessingService
from backend.workers.base import StageMessage, Worker

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def retry_delay_seconds(attempt: int, base: float, maximum: float) -> float:
    """Exponential backoff for the `attempt`th try, capped at `maximum`.

    A module-level pure function rather than an expression inlined in
    _handle_record so the progression can be asserted directly, without a
    test having to infer it from wall-clock time around real Kafka calls
    (which also pay consumer-group join and metadata-fetch cost, and so
    measure the harness as much as the backoff).

    attempt is 1-based, matching StageProcessingService's counter: the
    first retry waits `base`, the second 2x that, and so on.
    """
    return min(base * (2 ** (attempt - 1)), maximum)


def _extract_trace_context(headers: list[tuple[str, bytes]] | None) -> Context:
    """The other half of outbox_publisher.py's _inject_trace_headers():
    pulls the producer's span context back out of the Kafka message
    headers so this worker's "stage.process" span becomes a child of the
    same trace, not the start of a new, disconnected one."""
    carrier = {key: value.decode("utf-8") for key, value in (headers or [])}
    return propagate.extract(carrier)


class WorkerRunner:
    def __init__(
        self,
        worker: Worker,
        sessionmaker: async_sessionmaker,
        *,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
        retry_base_delay_seconds: float = 2.0,
        retry_max_delay_seconds: float = 30.0,
        max_poll_interval_seconds: float = 3600.0,
    ) -> None:
        self._worker = worker
        self._sessionmaker = sessionmaker
        self._topic = topic
        self._group_id = group_id
        self._bootstrap_servers = bootstrap_servers
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        self._max_poll_interval_seconds = max_poll_interval_seconds
        self._consumer: AIOKafkaConsumer | None = None
        self._dlq_producer: AIOKafkaProducer | None = None

    async def _ensure_started(self) -> None:
        # Constructed here, not in __init__: aiokafka clients need a running
        # event loop at construction time, and __init__ runs before
        # asyncio.run() starts one.
        if self._consumer is None:
            self._consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=self._bootstrap_servers,
                group_id=self._group_id,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                # The consume loop awaits the entire stage between polls, so
                # this is really "how long may one stage take". aiokafka
                # defaults it to 300s, which a slow render or merge can
                # exceed -- and the consequence is not an error but silent
                # duplication: evicted consumer, uncommitted offset,
                # message redelivered, the same work done again.
                max_poll_interval_ms=int(self._max_poll_interval_seconds * 1000),
            )
            await self._consumer.start()
            self._dlq_producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
            await self._dlq_producer.start()
            logger.info(
                "worker %r consuming topic=%r group=%r",
                self._worker.name,
                self._topic,
                self._group_id,
            )

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            await self._dlq_producer.stop()

    async def run_forever(self) -> None:
        await self._ensure_started()
        try:
            while True:
                await self.consume_one()
        finally:
            await self.stop()

    async def consume_one(self) -> None:
        """Fetches and fully handles exactly one message. Exposed separately
        from run_forever() so tests can drive the harness one delivery at a
        time and inspect state between them."""
        await self._ensure_started()
        record = await self._consumer.getone()
        await self._handle_record(record)

    async def _handle_record(self, record) -> None:
        message = self._parse(record.value)
        ctx = _extract_trace_context(record.headers)

        # The span's lifetime is deliberately scoped to just the processing
        # call, not the retry sleep/seek or DLQ dispatch below -- those are
        # harness reactions to the outcome, not part of "processing" the
        # stage.
        with tracer.start_as_current_span("stage.process", context=ctx) as span:
            span.set_attribute("job_id", str(message.job_id))
            span.set_attribute("stage", message.stage)

            async with self._sessionmaker() as session:
                result = await StageProcessingService(session, self._worker).handle(message)

            span.set_attribute("outcome", str(result.outcome))
            span.set_attribute("attempt", result.attempt)

        if result.outcome in (ProcessingOutcome.SUCCEEDED, ProcessingOutcome.ALREADY_DONE):
            await self._consumer.commit()
            return

        if result.outcome is ProcessingOutcome.RETRY:
            delay = retry_delay_seconds(
                result.attempt,
                self._retry_base_delay_seconds,
                self._retry_max_delay_seconds,
            )
            logger.warning(
                "stage failed; retrying with backoff: %s",
                result.error,
                extra={
                    "job_id": str(message.job_id),
                    "stage": message.stage,
                    "attempt": result.attempt,
                    "max_attempts": result.max_attempts,
                    "retry_delay_seconds": delay,
                },
            )
            await asyncio.sleep(delay)
            # Rewind the consumer's local fetch position so the next
            # getone() re-fetches this same record instead of moving on to
            # whatever comes next on the partition.
            self._consumer.seek(TopicPartition(record.topic, record.partition), record.offset)
            return  # offset NOT committed -> redelivered on this seek or a future restart

        # EXHAUSTED
        logger.error(
            "stage exhausted retries; sending to DLQ: %s",
            result.error,
            extra={
                "job_id": str(message.job_id),
                "stage": message.stage,
                "attempt": result.attempt,
                "max_attempts": result.max_attempts,
            },
        )
        await self._send_to_dlq(message, result.error)
        await self._consumer.commit()  # unblock the partition

    async def _send_to_dlq(self, message: StageMessage, error: str | None) -> None:
        dlq_topic = f"{self._topic}.dlq"
        payload = {
            "job_id": str(message.job_id),
            "stage": message.stage,
            "workflow": message.workflow,
            "payload": message.payload,
            "error": error,
        }
        await self._dlq_producer.send_and_wait(
            dlq_topic,
            key=str(message.job_id).encode("utf-8"),
            value=json.dumps(payload).encode("utf-8"),
        )

    @staticmethod
    def _parse(raw: bytes) -> StageMessage:
        data = json.loads(raw)
        return StageMessage(
            job_id=uuid.UUID(data["job_id"]),
            stage=data["stage"],
            workflow=data["workflow"],
            payload=data["payload"],
        )
