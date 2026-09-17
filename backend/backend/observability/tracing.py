"""OpenTelemetry tracing: one trace per job, spanning API submission ->
outbox publish -> Kafka -> worker processing, exported to Jaeger via OTLP.

Manual spans only, not auto-instrumentation packages -- matches how the
rest of this codebase is hand-built (the outbox pattern, the retry/DLQ
harness) rather than framework-driven, and keeps every span traceable to
a boundary someone explicitly chose rather than one a library decided was
interesting.

Kafka doesn't propagate trace context on its own, unlike an HTTP call
`opentelemetry-instrumentation-*` packages could intercept automatically.
The producer side (backend/messaging/outbox_publisher.py) injects the
current span's context into the Kafka message headers; the consumer side
(backend/workers/runner.py) extracts it back out before starting its own
span as a child of it. That's what stitches "API received the request"
and "worker finished processing it" into one trace instead of two.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from backend.core.config import Settings


def configure_tracing(settings: Settings, service_name: str) -> None:
    """Sets the process-wide TracerProvider. Called once per process, at
    startup, by both the API (backend/api/main.py) and the worker CLI
    (backend/workers/cli.py) -- each passes a different service_name so
    Jaeger's service dropdown tells them apart.
    """
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
