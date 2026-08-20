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
    # redelivered to a worker that starts it again from scratch.
    #
    # 300s is not hypothetical for this project: a find_content search is
    # paced by the vision provider's tokens-per-minute budget and takes
    # minutes by design, and a long render or merge can too. The failure is
    # nasty because it looks like nothing is wrong -- work simply repeats,
    # re-billing the vision API each time.
    #
    # An hour, because the cost of setting it too high is only that a
    # genuinely hung worker is noticed late, while the cost of too low is
    # duplicated paid work in a loop.
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

    # Vision runs on the same provider again, a third model. Hosted for the
    # same reason as whisper above: a local VLM would blow the 4GB
    # deployment target on its own (see the EC2 sizing notes). Llama-4
    # Scout and Maverick, the obvious alternatives, are both deprecated on
    # Groq. Hard provider limits: 5 images and 20MB per request.
    groq_vision_model: str = "qwen/qwen3.6-27b"

    # How often find_content looks at a frame, and the ceiling on how many
    # it ever looks at. Every frame is billed, so these are the cost dials.
    #
    # The worker samples at max(interval, duration / max_frames), which
    # makes cost O(1) in video length rather than linear -- a 3-hour upload
    # costs the same as a 10-minute one, just sampled more coarsely.
    # Without the cap a single long upload is an unbounded bill.
    #
    # Boundary accuracy is +/- half the effective interval. At these
    # defaults a 10-minute video costs 150 frames in 30 requests; a
    # 3-minute clip samples every 2s for ~1s accuracy.
    vision_sample_interval_seconds: float = 2.0
    vision_max_frames: int = 150

    # The other end of the same dial, and the one that is easy to leave
    # out. vision_max_frames stops a long video costing too much; this
    # stops a SHORT one being sampled too coarsely to see anything.
    #
    # A 12s clip at a flat 2s interval is six frames, so a subject visible
    # for one second either lands on a sample or is missed entirely, and
    # several brief appearances coalesce into one long range -- which makes
    # remove_matches cut footage the subject was never in, the opposite of
    # the intended bias. Short clips are therefore sampled at
    # duration / vision_min_frames instead, which binds below ~48s and
    # costs at most 24 frames (5 requests) no matter how short the clip.
    #
    # The floor stops that reaching absurdity on a 2s clip: below it,
    # frames are near-duplicates and each one is still billed.
    vision_min_frames: int = 24
    vision_min_sample_interval_seconds: float = 0.25

    # TWO limits collide here, and the smaller one wins.
    #
    # The model accepts at most 3 images ("Too many images provided. This
    # model supports up to 3 images" -- not the 5 the model card implies).
    # But rate limiting is accounted separately from billing: a request of
    # 3 images is estimated at 8,383 tokens against the free tier's 8,000
    # TPM and is rejected outright, at every frame width from 192 to 512 --
    # so the estimator charges ~2,786 per image regardless of resolution,
    # while the same image *bills* only ~845. Shrinking frames therefore
    # buys nothing; sending fewer per request is the only lever.
    #
    # Two fits (measured: 1,690 billed, accepted). Raise it to 3 on a tier
    # whose TPM has the headroom.
    vision_frames_per_request: int = 2

    # In-flight vision requests. One, because parallelism buys nothing
    # against a per-minute token budget: at ~2,786 estimated tokens per
    # image the free tier affords under three images a minute, so extra
    # concurrency only produces simultaneous rejections that then all wait
    # out the same window. Raise it with the tier -- it is latency, not
    # throughput, that concurrency fixes, and only once TPM is not binding.
    vision_request_concurrency: int = 1

    # Qwen3 is a reasoning model, and left alone it thinks at length before
    # answering: measured live, one 2-frame request spent 1,135 completion
    # tokens (4,410 characters of reasoning) to return a 2-entry verdict.
    # With reasoning off the same request costs 82 tokens and returns the
    # same answer -- a ~14x saving on the only billed part of the call.
    #
    # It also fixes a real failure rather than only a cost: with reasoning
    # on, json_object requests intermittently came back as a 400
    # `json_validate_failed` with an EMPTY failed_generation, because the
    # reasoning ran on instead of emitting the object. That is invisible in
    # tests -- every test injects a fake client.
    #
    # Blank rather than removed, so a swap to a non-reasoning vision model
    # (which would 400 on the parameter) needs a config change, not a code
    # change.
    vision_reasoning_effort: str = "none"

    # Generous: a 3-frame verdict is ~120 tokens with reasoning off. Exists
    # so a model that ignores the instruction and rambles gets cut off
    # rather than billing without limit -- a truncated reply fails the JSON
    # parse, which is the safe direction.
    vision_max_output_tokens: int = 1024

    # How many times a rate-limited batch waits for the token window to
    # refill before the stage gives up. Five, because the free tier's 8,000
    # TPM against a flat 922 tokens per image means even a short search
    # spans several windows -- see GroqVisionClient._create_with_pacing.
    vision_rate_limit_retries: int = 10

    # Hits the model reports below this are discarded. A hit with no
    # confidence at all counts as certain rather than being dropped --
    # absence of a score is not evidence of doubt.
    vision_min_confidence: float = 0.5

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
