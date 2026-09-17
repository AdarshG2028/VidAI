"""Structured JSON logging shared by every Setu process (API, outbox
publisher, workers).

One JSON object per line, everywhere: the API and each worker run as
separate OS processes, so the only way to reconstruct one job's journey
across all of them is to grep/join their log output by a shared field —
`job_id`, and once tracing (Part 3) is wired up, `trace_id`/`span_id` too.

The trace/span lookup happens here, in the formatter, rather than at each
call site: `opentelemetry.trace.get_current_span()` is safe to call even
before any TracerProvider is configured (it returns a no-op span whose
context is invalid), so this file needs no changes when Part 3 adds real
tracing — spans just start showing up in every log line once that lands.
"""

import json
import logging
from datetime import UTC, datetime

from opentelemetry import trace

from backend.core.config import Settings

# Standard attributes every LogRecord carries. Anything else on the record
# came from a caller's extra={...} and belongs in the JSON output.
_RESERVED_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Replaces the root logger's handlers with one JSON-formatted stream
    handler. Called once per process, at startup, by both the API
    (backend/api/main.py) and the worker CLI (backend/workers/cli.py)."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
