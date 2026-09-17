"""Application settings, loaded from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "setu"
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Comma-separated origins the browser may call this API from, e.g.
    # "https://your-app.vercel.app,http://localhost:3000". "*" (default)
    # allows any origin -- fine here because identity is a plain asserted
    # header (backend/api/deps.py), not a cookie/session, so there is no
    # credentialed-CORS footgun to widening this. Narrow it once a real
    # frontend origin exists, but nothing breaks by leaving it permissive
    # for local dev or an early deploy.
    cors_allowed_origins: str = "*"

    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://setu:setu@localhost:5432/setu",
        description="Async SQLAlchemy DSN; must use the asyncpg driver.",
    )
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 5

    # Redpanda advertises 19092 to the host and 9092 inside the compose
    # network; this default is for processes running on the host via uv.
    kafka_bootstrap_servers: str = "localhost:19092"

    outbox_poll_interval_seconds: float = 2.0
    outbox_batch_size: int = 50
    outbox_max_publish_attempts: int = 10
    outbox_publish_timeout_seconds: float = 10.0

    # How long a worker may spend on ONE message before Kafka assumes it
    # died. aiokafka's default is 300s, and the consume loop awaits the
    # whole stage between polls -- so any stage slower than that gets the
    # consumer evicted, the offset never commits, and the message is
    # redelivered to a worker that starts it again from scratch. Nasty
    # because it looks like nothing is wrong -- work simply repeats.
    #
    # An hour, because the cost of setting it too high is only that a
    # genuinely hung worker is noticed late, while the cost of too low is
    # duplicated work in a loop -- and a long render or merge can
    # legitimately run for minutes.
    worker_max_poll_interval_seconds: float = 3600.0

    worker_retry_base_delay_seconds: float = 2.0
    worker_retry_max_delay_seconds: float = 30.0

    # How often the API refreshes the job-lifecycle Prometheus gauges
    # (setu_jobs_pending/processing/completed/failed) from Postgres.
    metrics_poll_interval_seconds: float = 5.0

    # Jaeger's OTLP/grpc receiver. Like kafka_bootstrap_servers above, this
    # is the host-side address -- the API and workers run on the host via
    # uv, not in compose.
    otel_exporter_otlp_endpoint: str = "localhost:4317"
    # Off in environments with no Jaeger/OTLP collector reachable (e.g. a
    # cloud deploy) -- tracing wouldn't crash without one (the exporter
    # batches in a background thread), but it would spam connection-refused
    # errors into the logs forever.
    tracing_enabled: bool = True

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Local-disk Storage backend's root directory. Swapping to an
    # S3-compatible backend later is a new Storage implementation behind
    # the same interface, not a change to this setting's callers.
    storage_local_path: str = "./data/storage"

    # "local" (default) keeps every environment without S3 credentials
    # working exactly as before -- tests, and local dev, never need to
    # touch these. Flip to "s3" only once storage_s3_bucket points at a
    # real, empty bucket: switching backends does not migrate existing
    # local:// URIs already recorded in videos/results/video_assets.
    storage_backend: Literal["local", "s3"] = "local"
    storage_s3_bucket: str = ""
    storage_s3_region: str = "ap-south-1"
    # Left None for real AWS S3, which needs no override. Setting this is
    # what would let the same S3Storage target R2/MinIO/B2 later without
    # a code change -- the whole reason boto3 was chosen over an
    # AWS-specific SDK.
    storage_s3_endpoint_url: str | None = None
    storage_s3_access_key_id: str = ""
    storage_s3_secret_access_key: str = ""
    # How long a presigned download URL stays valid. Short: it is minted
    # fresh on every /artifacts request (see require_artifact_access),
    # never stored, so there is no reason for it to outlive one viewing
    # session.
    storage_s3_presigned_url_ttl_seconds: int = 3600

    # Most recent N messages passed to the planner as context -- a real
    # prompt-budget decision (grows the LLM prompt linearly), so it's a
    # named setting rather than a hardcoded number.
    conversation_context_limit: int = 20

    # The planner's provider (Changelog v8). Model must be on Groq's
    # structured-outputs supported-models list (response_format=json_schema)
    # -- see https://console.groq.com/docs/structured-outputs#supported-models.
    groq_api_key: str = ""
    # llama-3.3-70b-versatile does NOT support response_format=json_schema
    # despite being Groq's most commonly used default (confirmed live,
    # 2026-07-28: 400 invalid_request_error) -- openai/gpt-oss-20b does.
    groq_model: str = "openai/gpt-oss-20b"
    # Speech-to-text runs on the same provider as planning but a different
    # model. whisper-large-v3 is the same checkpoint one would run locally
    # (Changelog v11) -- hosting it removes the model download and the RAM
    # it needs, without trading transcription quality.
    groq_transcription_model: str = "whisper-large-v3"

    # x264 speed/size trade for full-quality renders. libx264's own default
    # is "medium"; measured on a 140s 1080p clip, "veryfast" halved the
    # encode (42s -> 23s) and produced a *smaller* file (2.6M -> 2.4M),
    # so the usual speed-for-size trade doesn't apply at this point on the
    # curve. Ignored by encoders that don't take it (vp9, gif), so it is
    # safe to pass unconditionally. Preview mode overrides it with
    # ultrafast.
    video_encode_preset: str = "veryfast"

    # Intermediate artifacts are kept this long after a job finishes, then
    # swept. Not zero: every stage's output is what makes "which step went
    # wrong" answerable, and a preview is worth re-watching. But nothing
    # deleted anything at all before this, and each stage stores a full
    # copy -- a 6-stage job on a 100MB source leaves ~600MB forever.
    artifact_retention_hours: float = 24.0
    artifact_cleanup_interval_seconds: float = 900.0
    artifact_cleanup_enabled: bool = True

    # How often the room socket's progress bridge re-reads the jobs of
    # rooms that currently have a listener. Short, because this is what
    # replaces per-client polling of GET /jobs/{id} and a stage boundary
    # a user is watching for should not sit unreported for long; cheap,
    # because a room nobody is watching is not queried at all.
    room_progress_poll_interval_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()
