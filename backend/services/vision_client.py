"""VisionClient -- the seam between the find_content capability and a
vision-language provider, mirroring transcription_client.py (which itself
mirrors llm_client.py) for the same reason: the worker never imports a
provider SDK, so swapping provider, or moving to a locally-run VLM, is a
new class behind this interface rather than a change to the worker.

Groq hosts `qwen/qwen3.6-27b`, a 27B multimodal model. Hosted rather than
local for exactly the reason Whisper is (Changelog v11): a local VLM of
useful quality would blow the 4GB deployment target on its own, and this
is a feature nobody wants to double the machine for. Llama-4 Scout and
Maverick, the obvious alternatives, are both deprecated on Groq.

**JSON contract: `json_object`, not `json_schema` -- verified, not
assumed.** Groq's structured-outputs support list covers only GPT-OSS 20B
and 120B; qwen3.6-27b appears on neither the strict nor the best-effort
list. `llm_client.py` records this repo already being burned by assuming a
Groq model supported json_schema when it did not (a live 400), so the
capability was checked against the provider's own table before choosing.
json_object guarantees only *syntactically valid* JSON with no schema
enforcement, which is why the response is parsed defensively below and why
the prompt states the required shape explicitly -- json_object mode
requires the instruction to be in the prompt.

**Provider hard limits: 5 images and 20MB per request.** Both are enforced
by the caller (see content_search_worker's batching); exceeding either is
a 400.
"""

import asyncio
import base64
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import groq

logger = logging.getLogger(__name__)

# No single rate-limit wait may exceed this, however long the provider says
# the window is. A job that silently stalls for an hour is worse than one
# that fails and says why.
_MAX_RETRY_WAIT_SECONDS = 90.0

__all__ = [
    "FrameMatch",
    "GroqVisionClient",
    "UnusableFramesError",
    "VisionClient",
    "VisionError",
]


class VisionError(Exception):
    """The provider could not be reached, or failed in a way a retry might
    fix. Mapped by the worker onto a retryable MediaProcessingError.

    Never swallowed into "no matches in those frames": the coalescer bridges
    small gaps, so a silently-dropped batch would be papered over and the
    stage would emit ranges that are confidently, invisibly wrong.
    """


class UnusableFramesError(VisionError):
    """The frames themselves are the problem -- too large for the provider,
    or rejected outright. Retrying sends identical bytes, so the worker maps
    this to a permanent failure instead."""


@dataclass(frozen=True)
class FrameMatch:
    """One frame's verdict, keyed by the id the caller assigned.

    Keyed rather than positional on purpose. The realistic provider failure
    is returning fewer entries than images sent; zipping by position would
    then shift every later frame's timestamp and cut the wrong footage,
    with nothing in the output to show it happened.
    """

    frame_id: str
    visible: bool
    # 1.0 when the model gave no score. Absence of a score is not evidence
    # of doubt, and dropping unscored hits would silently bias the search
    # toward finding nothing.
    confidence: float = 1.0


class VisionClient(ABC):
    @abstractmethod
    async def inspect_frames(
        self, frames: list[tuple[str, bytes]], *, query: str
    ) -> list[FrameMatch]:
        """Decide, per frame, whether `query` is visible in it.

        `frames` is (frame_id, jpeg_bytes), at most
        settings.vision_frames_per_request long. Returns one FrameMatch per
        frame the provider reported on -- callers must treat a missing id
        as "not visible" rather than assuming positional alignment.

        Raises VisionError on an infrastructure failure, UnusableFramesError
        when the provider rejects the images themselves.
        """
        raise NotImplementedError


_SYSTEM = (
    "You are a precise visual inspector. You are shown labelled frames from "
    "a video. For each frame independently, decide whether the described "
    "subject is clearly visible in that frame. Judge only what you can see "
    "in that one frame; do not infer from neighbouring frames. When unsure, "
    "answer false."
)


def _is_rate_limit(exc: "groq.APIStatusError") -> bool:
    """Whether a status error is a rate limit rather than a bad request.

    Status alone is not enough: Groq returns 413 for a token-per-minute
    overage, which collides with the genuine "payload too large" case. The
    body carries the distinction (`code: rate_limit_exceeded`, `type:
    tokens`), so it is read from the structured body where available and
    from the string only as a fallback.
    """
    if exc.status_code == 429:
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            if error.get("code") == "rate_limit_exceeded" or error.get("type") == "tokens":
                return True
    return "rate_limit_exceeded" in str(exc)


def _is_malformed_generation(exc: "groq.APIStatusError") -> bool:
    """Whether the model emitted invalid JSON on this attempt.

    json_object mode does NOT guarantee valid JSON -- it makes the provider
    *check*, and return a 400 `json_validate_failed` when the check fails.
    Observed live, mid-search:

        {"frames": [{"id": "f20", ...}], [{"id": "f21", ...}]}

    which is a malformed second array rather than a second element. That is
    a property of one sampled generation, not of the frames: the same
    images asked again almost always parse. Classified as permanent -- and
    every 400 was, before this -- it dead-letters a search that has already
    paid for every frame inspected up to that point.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == "json_validate_failed":
            return True
    return "json_validate_failed" in str(exc)


_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?")


def _parse_duration(value: str) -> float | None:
    """Groq reports reset times as "19.44s", "2m59.56s", "14m24s"."""
    match = _DURATION_RE.fullmatch(value.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _retry_after_seconds(exc: "groq.APIStatusError", attempt: int) -> float:
    """How long to wait before re-sending a rate-limited batch.

    Read from the provider rather than guessed: the response carries
    retry-after and x-ratelimit-reset-tokens, which say when the window
    actually refills (verified live -- the latter reads e.g. "19.44s").
    Exponential backoff is only the fallback for a response carrying
    neither, since guessing short re-fails immediately and guessing long
    stalls a room that is watching the job.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(name)
        if not raw:
            continue
        seconds = _parse_duration(str(raw))
        if seconds is None:
            try:
                seconds = float(raw)
            except (TypeError, ValueError):
                continue
        # A tiny reported reset still needs a moment for the window to tick
        # over, and no single wait may exceed the cap.
        return min(max(seconds + 0.5, 1.0), _MAX_RETRY_WAIT_SECONDS)
    return min(2.0**attempt, _MAX_RETRY_WAIT_SECONDS)


def _build_user_content(frames: list[tuple[str, bytes]], query: str) -> list[dict]:
    """The text part names every frame id and states the JSON shape.

    Both are load-bearing. The ids are what the response is keyed on, and
    json_object mode enforces no schema at all -- the provider's own docs
    require the JSON instruction to appear in the prompt, so the shape is
    spelled out here rather than left to response_format alone.
    """
    ids = ", ".join(frame_id for frame_id, _ in frames)
    parts: list[dict] = [
        {
            "type": "text",
            "text": (
                f'Subject to find: "{query}"\n'
                f"Frames, in order: {ids}.\n"
                "Reply with JSON only, in exactly this shape, with one entry "
                'per frame id listed above and nothing else:\n'
                '{"frames": [{"id": "<frame id>", "visible": true, '
                '"confidence": 0.0}]}'
            ),
        }
    ]
    for _, jpeg in frames:
        encoded = base64.b64encode(jpeg).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    return parts


class GroqVisionClient(VisionClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: str = "none",
        max_tokens: int = 1024,
        rate_limit_retries: int = 5,
    ) -> None:
        self._client = groq.AsyncGroq(api_key=api_key)
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._rate_limit_retries = rate_limit_retries

    async def inspect_frames(
        self, frames: list[tuple[str, bytes]], *, query: str
    ) -> list[FrameMatch]:
        if not frames:
            return []

        start = time.perf_counter()
        response = await self._create_with_pacing(frames, query)
        matches = _parse_matches(response, {frame_id for frame_id, _ in frames})
        logger.info(
            "vision batch inspected",
            extra={
                "provider": "groq",
                "model": self._model,
                "latency_seconds": time.perf_counter() - start,
                "frames_sent": len(frames),
                "verdicts": len(matches),
            },
        )
        return matches

    async def _create_with_pacing(self, frames: list[tuple[str, bytes]], query: str):
        """One request, waiting out rate limits rather than failing on them.

        Paced here rather than left to the stage's own retry, because a
        rate limit is not a failure of this batch -- it is the account's
        per-minute budget, and a search legitimately needs more than one
        minute of it. Measured on the free tier: an image costs a flat 922
        prompt tokens regardless of its resolution, so even a 24-frame
        search is ~22k tokens against an 8,000 TPM limit and cannot finish
        inside one window at any concurrency.

        Failing the stage instead would restart from frame zero and re-bill
        every frame already inspected, since find_content is deliberately
        uncached (two queries on one video would overwrite each other).
        """
        for attempt in range(self._rate_limit_retries + 1):
            try:
                return await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": _build_user_content(frames, query)},
                    ],
                    # json_object, not json_schema -- see the module docstring.
                    response_format={"type": "json_object"},
                    max_tokens=self._max_tokens,
                    # Reasoning off: this is a classification, not a problem
                    # to think through, and leaving it on both costs ~14x the
                    # completion tokens and intermittently overruns into a
                    # 400 json_validate_failed with an empty generation.
                    # Omitted when blank, so a non-reasoning model can be
                    # swapped in from config without a 400 on the parameter.
                    **(
                        {"reasoning_effort": self._reasoning_effort}
                        if self._reasoning_effort
                        else {}
                    ),
                )
            except groq.APIStatusError as exc:
                # Rate limits are checked BEFORE the status code, because Groq
                # answers a token-per-minute overage with 413 rather than 429 --
                # and 413 is otherwise a permanent "these images are too big".
                # Observed live: "Request too large ... on tokens per minute
                # (TPM): Limit 8000, Requested 8388", code rate_limit_exceeded.
                # Classified by status alone, that dead-letters the job on a
                # condition which clears by itself in twenty seconds.
                if _is_rate_limit(exc):
                    if attempt >= self._rate_limit_retries:
                        raise VisionError(
                            "groq rate limited this request and it had not cleared "
                            f"after {self._rate_limit_retries} waits: {exc}"
                        ) from exc
                    delay = _retry_after_seconds(exc, attempt)
                    logger.warning(
                        "vision request rate limited; waiting for the window to reset",
                        extra={
                            "attempt": attempt + 1,
                            "attempts_allowed": self._rate_limit_retries,
                            "waiting_seconds": round(delay, 2),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                # A malformed generation is a property of this sample, not
                # of the frames -- asking again re-rolls it. Retried here
                # rather than at the stage, for the same reason as a rate
                # limit: restarting the stage re-bills every frame already
                # inspected, and find_content is deliberately uncached.
                if _is_malformed_generation(exc) and attempt < self._rate_limit_retries:
                    logger.warning(
                        "vision provider returned invalid JSON; asking again",
                        extra={"attempt": attempt + 1},
                    )
                    continue
                # 413 (too large) and 400/415/422 (rejected/unsupported)
                # describe the images, not the connection: identical bytes
                # fail identically on every retry.
                if exc.status_code in (400, 413, 415, 422):
                    raise UnusableFramesError(
                        f"groq rejected the frames ({exc.status_code}): {exc}"
                    ) from exc
                raise VisionError(f"groq vision request failed: {exc}") from exc
            except groq.APIError as exc:
                raise VisionError(f"groq vision request failed: {exc}") from exc
        raise VisionError("groq vision request exhausted its rate-limit waits")


def _parse_matches(response: object, known_ids: set[str]) -> list[FrameMatch]:
    """Read verdicts off the SDK response, defensively.

    json_object guarantees syntactic validity and nothing else, so every
    field here is treated as untrusted: a missing `frames` key, an entry
    that is not a dict, an id the caller never sent, a non-numeric
    confidence. Anything unrecognised is dropped rather than guessed at --
    the caller treats an id it hears nothing about as "not visible", which
    is the safe direction for a search that drives a cut.
    """
    import json

    try:
        content = response.choices[0].message.content or ""
        data = json.loads(content)
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise VisionError(f"groq vision returned unparseable JSON: {exc}") from exc

    raw = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise VisionError("groq vision response had no 'frames' list")

    matches: list[FrameMatch] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        frame_id = item.get("id")
        if not isinstance(frame_id, str) or frame_id not in known_ids or frame_id in seen:
            continue
        seen.add(frame_id)
        confidence = item.get("confidence")
        matches.append(
            FrameMatch(
                frame_id=frame_id,
                visible=bool(item.get("visible")),
                confidence=(
                    float(confidence)
                    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                    else 1.0
                ),
            )
        )
    return matches
