"""TranscriptionClient -- the seam between the transcribe capability and a
speech-to-text provider, mirroring llm_client.py's LLMClient/GroqClient
split for the same reason: the worker never imports a provider SDK, so
swapping provider (or moving back to a locally-run Whisper) is a new class
behind this interface rather than a change to the worker.

Groq hosts `whisper-large-v3` -- the same weights as running Whisper
locally, so nothing is traded on transcription quality; only the machine
doing the inference changes. That removes a ~3GB model download and
1.5-3GB of resident RAM from every deployment (architecture doc,
Changelog v11).
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import groq

logger = logging.getLogger(__name__)

__all__ = [
    "GroqTranscriptionClient",
    "TranscriptionClient",
    "TranscriptionError",
    "TranscriptionResult",
    "TranscriptSegment",
    "UnusableAudioError",
]


class TranscriptionError(Exception):
    """The provider could not be reached, or failed in a way a retry might
    fix. Mapped by the worker onto a retryable MediaProcessingError."""


class UnusableAudioError(TranscriptionError):
    """The audio itself is the problem -- too large for the provider, or
    rejected outright. Retrying sends the identical bytes, so the worker
    maps this to a permanent failure instead."""


@dataclass(frozen=True)
class TranscriptSegment:
    """One timed run of speech. Seconds from the start of the audio, which
    is what SRT cues need."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None
    segments: list[TranscriptSegment]


class TranscriptionClient(ABC):
    @abstractmethod
    async def transcribe(
        self, audio: bytes, *, filename: str, language: str | None = None
    ) -> TranscriptionResult:
        """Transcribe encoded audio bytes. Raises TranscriptionError on an
        infrastructure failure, UnusableAudioError when the audio itself is
        rejected."""
        raise NotImplementedError


class GroqTranscriptionClient(TranscriptionClient):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = groq.AsyncGroq(api_key=api_key)
        self._model = model

    async def transcribe(
        self, audio: bytes, *, filename: str, language: str | None = None
    ) -> TranscriptionResult:
        start = time.perf_counter()
        try:
            response = await self._client.audio.transcriptions.create(
                file=(filename, audio),
                model=self._model,
                # verbose_json is what carries per-segment timings; the
                # default response is a bare transcript string, which
                # cannot produce subtitle cues.
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                # Omitted rather than guessed when unknown -- Whisper
                # detects the language itself, and forcing the wrong one
                # degrades the transcript badly.
                **({"language": language} if language else {}),
            )
        except groq.APIStatusError as exc:
            # 413 (too large) and 400 (rejected/unsupported) describe the
            # audio, not the connection: identical bytes fail identically
            # on every retry.
            if exc.status_code in (400, 413, 415, 422):
                raise UnusableAudioError(
                    f"groq rejected the audio ({exc.status_code}): {exc}"
                ) from exc
            raise TranscriptionError(f"groq transcription failed: {exc}") from exc
        except groq.APIError as exc:
            raise TranscriptionError(f"groq transcription failed: {exc}") from exc

        latency_seconds = time.perf_counter() - start
        segments = _parse_segments(response)
        logger.info(
            "transcription completed",
            extra={
                "provider": "groq",
                "model": self._model,
                "latency_seconds": latency_seconds,
                "segment_count": len(segments),
                "detected_language": getattr(response, "language", None),
            },
        )
        return TranscriptionResult(
            text=(getattr(response, "text", "") or "").strip(),
            language=getattr(response, "language", None),
            segments=segments,
        )


def _parse_segments(response: object) -> list[TranscriptSegment]:
    """Read segments off the SDK response.

    Accessed defensively via getattr/dict lookups: the SDK returns a typed
    object for verbose_json but the field is only populated when timestamp
    granularities were requested, and a response with no speech in it
    carries no segments at all.
    """
    raw = getattr(response, "segments", None) or []
    segments: list[TranscriptSegment] = []
    for item in raw:
        get = item.get if isinstance(item, dict) else lambda key, _d=None, _i=item: getattr(_i, key, _d)
        text = (get("text", "") or "").strip()
        if not text:
            continue
        try:
            start, end = float(get("start", 0.0)), float(get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end > start:
            segments.append(TranscriptSegment(start=start, end=end, text=text))
    return segments
