#!/bin/sh
#
# Role selector for the single application image (see ../Dockerfile).
#
#   api                     the FastAPI app
#   worker <stage>          one stage worker, e.g. `worker crop`
#   migrate                 alembic upgrade head, then exit
#   <anything else>         run verbatim, so `docker run <image> sh` works
#
# `exec` on every terminal command so the process replaces this shell and
# receives SIGTERM directly from tini. Without it the signal is delivered
# to /bin/sh, which ignores it, and docker stop escalates to SIGKILL after
# its grace period -- killing a worker mid-encode rather than letting it
# finish the message it is holding.

set -eu

role="${1:-api}"

case "$role" in
  api)
    # Off by default. Migrations are schema changes for the whole
    # deployment, not per-container startup work: with more than one
    # replica every one of them races the same `alembic upgrade head`,
    # and a container that cannot reach the database at boot fails to
    # start even when the schema was already current. Run them as their
    # own step (`docker compose run --rm api migrate`) and leave this
    # unset; RUN_MIGRATIONS=true is the escape hatch for a single-box
    # deploy where that extra step is not worth it.
    if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
      echo "entrypoint: running alembic upgrade head"
      alembic upgrade head
    fi
    # PORT is honoured because some hosts (Render, Fly, Cloud Run) inject
    # it and expect the app to bind exactly there.
    exec uvicorn backend.api.main:app \
      --host "${API_HOST:-0.0.0.0}" \
      --port "${PORT:-8000}"
    ;;

  worker)
    shift
    if [ "$#" -lt 1 ]; then
      echo "entrypoint: 'worker' needs a stage name, e.g. 'worker crop'" >&2
      exit 64
    fi
    stage="$1"
    shift
    # Topic and worker name are the same string by convention
    # (backend/workers/cli.py's WORKERS keys are the topic names). Any
    # extra arguments are passed through, so --group-id and friends still
    # work: `worker crop --group-id something`.
    #
    # Each worker serves its own Prometheus endpoint. The default is fine
    # in compose, where every worker is its own container and therefore
    # its own network namespace -- override METRICS_PORT only when running
    # several workers that genuinely share one (host networking, or
    # several processes in one container).
    exec python -m backend.workers.cli "$stage" \
      --worker "$stage" \
      --metrics-port "${METRICS_PORT:-9100}" \
      "$@"
    ;;

  migrate)
    exec alembic upgrade head
    ;;

  *)
    exec "$@"
    ;;
esac
