"""Does no real AI work — exists to prove the harness's reliability
guarantees (idempotency, retry, DLQ, crash recovery) independent of any
actual model.

Two flags in the job's payload, read only by this worker (the harness and
Worker interface don't know about them), support deterministic testing:
  _hang_seconds: sleep before finishing, so a test can SIGKILL the worker
                 process at a known point mid-processing.
  _fail: raise instead of succeeding, to exercise retry/DLQ.
"""

import asyncio
from typing import Any

from backend.workers.base import StageMessage, Worker


class DummyWorker(Worker):
    name = "dummy"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        hang_seconds = float(message.payload.get("_hang_seconds", 0))
        if hang_seconds:
            await asyncio.sleep(hang_seconds)

        if message.payload.get("_fail"):
            raise RuntimeError("DummyWorker: simulated failure")

        return {"processed_by": self.name, "job_id": str(message.job_id)}
