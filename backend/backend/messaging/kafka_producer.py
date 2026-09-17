"""Producer construction. Connection is deferred — see OutboxPublisher.ensure_started."""

from aiokafka import AIOKafkaProducer

from backend.core.config import get_settings


def build_producer() -> AIOKafkaProducer:
    settings = get_settings()
    return AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        acks="all",
        enable_idempotence=True,
    )
