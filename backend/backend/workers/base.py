"""Common Worker interface every stage worker implements.

The Kafka consumer harness (runner.py) and, later, the Phase 4 workflow
engine only ever talk to this interface — never to a specific AI model or
stage's internals. Adding a worker means adding a class here, not touching
the harness or the engine.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class StageMessage:
    """One dispatched stage of one job, as read off its Kafka topic."""

    job_id: UUID
    stage: int
    workflow: list[str]
    payload: dict[str, Any]


class PermanentError(Exception):
    """Raise instead of a plain Exception when the same message would fail
    identically on every redelivery (e.g. the input itself is unusable) —
    the harness sends it straight to the DLQ on the first attempt instead
    of burning the full retry budget's backoff on a foregone conclusion.
    Reserve it for failures the *input* causes, not the environment: a
    worker's transient/infra failures (a dependency briefly unreachable)
    should stay a plain Exception so they still get retried.
    """


class Worker(ABC):
    """Processes exactly one stage. Does not advance to the next stage —
    that dispatch belongs to the Phase 4 workflow engine, not this worker.

    Raise from process() to signal failure; the harness owns retry/DLQ
    decisions, not the worker. Raise PermanentError specifically when a
    retry is guaranteed to fail the same way.
    """

    name: str

    @abstractmethod
    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Do the stage's work and return the result payload to persist.

        previous_output is the prior stage's Result payload (None for the
        first stage), fetched and injected by StageProcessingService — a
        worker never queries the database itself, keeping it a stateless
        function of its inputs.
        """
