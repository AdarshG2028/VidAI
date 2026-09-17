"""LLMClient (Changelog v8) -- the one seam between the planner and a
concrete provider SDK. Only `GroqClient` exists for now; other providers
aren't stubbed out ahead of an actual second integration (per the v6
"no speculative generality" principle) -- adding one later is a new class
behind this same interface, not a change to the planner.
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import groq

from backend.services.prompt_builder import Prompt

logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    """Infrastructure-layer failure: network error, provider timeout, or
    non-parseable structured output. Distinct from a well-formed but
    semantically-invalid proposal, which validate_proposal catches instead
    -- the planner's infrastructure retry is built around this exception."""


class LLMClient(ABC):
    @abstractmethod
    async def complete(
        self, prompt: Prompt, *, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Returns the parsed structured-output JSON object matching
        response_schema. Raises LLMClientError on any infrastructure
        failure."""
        raise NotImplementedError


class GroqClient(LLMClient):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = groq.AsyncGroq(api_key=api_key)
        self._model = model

    async def complete(
        self, prompt: Prompt, *, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        messages = [{"role": "system", "content": prompt.system}, *prompt.messages]

        start = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "planner_response",
                        "schema": response_schema,
                        # Not strict: "strict": true requires
                        # additionalProperties: false on every nested
                        # object, which can't represent ProposalStage.params
                        # (deliberately an open dict -- each stage's real
                        # shape is checked at runtime by validate_proposal
                        # against the capability registry, not by this
                        # schema). Confirmed live against Groq, 2026-07-28:
                        # strict mode 400s on this schema; non-strict still
                        # guides the model into valid, schema-shaped JSON.
                        "strict": False,
                    },
                },
            )
        except groq.APIError as exc:
            raise LLMClientError(f"groq request failed: {exc}") from exc
        latency_seconds = time.perf_counter() - start

        content = completion.choices[0].message.content
        usage = completion.usage
        logger.info(
            "llm_client completion",
            extra={
                "provider": "groq",
                "model": self._model,
                "latency_seconds": latency_seconds,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            },
        )

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMClientError(f"malformed structured output: {exc}") from exc
