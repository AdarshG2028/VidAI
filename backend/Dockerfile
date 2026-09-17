# syntax=docker/dockerfile:1.7
#
# One image, two roles: the API and every stage worker run the same code
# and the same dependency set, so building them separately would mean two
# images to keep in lockstep for no gain. Which role a container plays is
# chosen by the command (see docker/entrypoint.sh):
#
#   docker run <image>                    -> the API
#   docker run <image> worker crop        -> the `crop` stage worker
#   docker run <image> migrate            -> alembic upgrade head, then exit
#
# Multi-stage: uv, the build cache and the lockfile resolution stay in the
# builder, so none of them ship. The runtime image carries the virtualenv,
# the application, and ffmpeg -- nothing else.

##############################################################################
# builder
##############################################################################
FROM python:3.13-slim-bookworm AS builder

# Pinned rather than :latest -- a build that silently picks up a new
# resolver is not reproducible, which is most of the point of --frozen.
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies as their own layer, resolved from the lockfile *before* any
# source is copied, so editing a worker does not reinstall asyncpg. The
# previous Dockerfile did `COPY . .` first, which meant every commit paid
# for a full dependency install.
#
# --no-install-project: the project itself is deliberately left out here;
# installing it needs the source, which would defeat the caching this
# layer exists for. It is installed in the second sync below.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# alembic.ini resolves script_location relative to itself
# (%(here)s/backend/database/migrations), so both it and backend/ have to
# be present for `migrate` to work at runtime.
# README.md is not documentation here: pyproject.toml declares
# `readme = "README.md"`, so the wheel build fails without it.
COPY pyproject.toml uv.lock alembic.ini README.md ./
COPY backend/ ./backend/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

##############################################################################
# runtime
##############################################################################
FROM python:3.13-slim-bookworm AS runtime

# ffmpeg is a runtime dependency, not a build tool: video_analysis probes
# with ffprobe and every media capability shells out to ffmpeg directly
# (backend/workers/media.py). There is no Python wrapper package, so it
# cannot come from pyproject.toml. Without it those workers raise
# MediaProcessingError on every message until the retry budget is spent.
#
# tini as PID 1: workers spawn ffmpeg as a child process, and PID 1 in a
# container gets no default signal handlers -- without an init, SIGTERM on
# `docker stop` is ignored and the container is SIGKILLed 10s later,
# mid-encode. tini forwards signals and reaps orphans.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root. Nothing here needs to write outside /app/data, and a container
# escape that lands as root on the host is the reason this is not a
# detail. Fixed uid/gid so a bind-mounted host volume has predictable
# ownership.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv ./.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Uploads and rendered output land here (Settings.storage_local_path).
# Created and owned up front so the directory exists even when nothing is
# mounted over it -- but in any real deployment this MUST be a mounted
# volume, or every render is lost on the next `docker compose up`.
# Deliberately not a VOLUME instruction: that would force an anonymous
# volume on every run and quietly accumulate orphans.
RUN mkdir -p /app/data/storage && chown -R app:app /app/data

USER app

EXPOSE 8000

# No HEALTHCHECK here on purpose: this image is also every worker, and a
# worker has no HTTP port to probe -- a Dockerfile-level check would mark
# 16 healthy containers as unhealthy. The API's check belongs next to the
# api service in compose, where it applies only to that service.

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["api"]
