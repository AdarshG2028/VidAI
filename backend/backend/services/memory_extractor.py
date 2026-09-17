"""MemoryExtractor — reads a finished conversation and decides whether it
revealed anything worth remembering about the user (Phase 6).

One stateless LLM call, per the §5 AI principles: no vector store, no RAG,
no episodic memory. The entire memory of this product is the handful of
columns on `user_preferences`, and this is the only thing that writes them.

The distinction it exists to draw is **durable preference vs one-off
instruction**, and it is the whole difficulty of the phase. "Always export
for LinkedIn" should be remembered; "crop this one to 9:16" must not be,
or the next unrelated video silently gets cropped because the assistant
"remembered" something that was never a preference. The prompt is
therefore written to prefer omission: a field left null means "no durable
evidence", which is the safe answer.
"""

import json
import logging
import uuid
from dataclasses import dataclass

from backend.models import Message, MessageRole
from backend.services.llm_client import LLMClient, LLMClientError
from backend.services.prompt_builder import Prompt

logger = logging.getLogger(__name__)

__all__ = ["ExtractedPreferences", "MemoryExtractor", "PREFERENCE_SCHEMA"]

_SYSTEM = """\
You review a finished conversation between a user and a video-editing \
assistant, and report only durable preferences the user expressed about \
how they like their videos handled.

A durable preference is a standing rule that should apply to future, \
unrelated videos. Look for words like "always", "I prefer", "from now on", \
"I usually", or a preference stated as a general habit.

A one-off instruction is about this video only, and must NOT be reported. \
"Crop this to 9:16", "make this one brighter", "add captions to this clip" \
are all one-off, even though they mention things the fields below can hold.

Report a field only when the conversation gives clear evidence for it. \
Leave it null otherwise. Null is always the correct answer when unsure — \
a wrongly remembered preference silently changes every future edit the \
user makes, which is far worse than remembering nothing.

Never infer a preference from what the assistant proposed or did. Only \
from what the user actually said about their own preferences.\
"""

# Mirrors the columns on UserPreference. Every field is nullable because
# "no durable evidence" is the expected answer for most conversations.
PREFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "preferred_platform": {
            "type": ["string", "null"],
            "description": "Where they publish, e.g. linkedin, tiktok, youtube, instagram.",
        },
        "preferred_export_format": {
            "type": ["string", "null"],
            "description": "Container they always want, e.g. mp4, mov, webm.",
        },
        "captions_enabled": {
            "type": ["boolean", "null"],
            "description": "True only if they said they always want captions/subtitles.",
        },
        "preferred_resolution": {
            "type": ["string", "null"],
            "description": "Resolution they always want, e.g. 1080p, 720p.",
        },
        "subtitle_language": {
            "type": ["string", "null"],
            "description": "Language code they always want subtitles in, e.g. en, es.",
        },
    },
    "required": [],
}

_FIELDS = tuple(PREFERENCE_SCHEMA["properties"])

# Models routinely express "no value" as a *word* rather than JSON null,
# especially when a schema marks a field nullable. Observed live: a
# conversation that never mentioned subtitles came back with
# subtitle_language="null", which stored cleanly and would then have been
# rendered into every future prompt as though the user had chosen it.
_NULLISH = frozenset(
    {"null", "none", "nil", "n/a", "na", "unknown", "unspecified", "not specified", "-"}
)


@dataclass(frozen=True)
class ExtractedPreferences:
    """Only the fields the conversation actually gave evidence for.

    Deliberately a sparse dict rather than a full record: absence must be
    distinguishable from "explicitly nothing", because applying it
    overwrites stored preferences, and a null-filled record would erase
    everything the user established in earlier sessions.
    """

    values: dict[str, object]

    def __bool__(self) -> bool:
        return bool(self.values)


class MemoryExtractor:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def extract(
        self, messages: list[Message], *, user_id: uuid.UUID
    ) -> ExtractedPreferences:
        transcript = _render(messages)
        if not transcript:
            return ExtractedPreferences({})

        prompt = Prompt(
            system=_SYSTEM,
            messages=[{"role": "user", "content": transcript}],
        )
        try:
            raw = await self._client.complete(prompt, response_schema=PREFERENCE_SCHEMA)
        except LLMClientError as exc:
            # Deliberately not re-raised. The job itself succeeded; failing
            # to learn from it is a soft failure and must not surface to
            # the user as a failed request (architecture doc, Phase 6
            # Risks).
            logger.warning(
                "memory extraction failed; preferences left unchanged",
                extra={"user_id": str(user_id), "error": str(exc)},
            )
            return ExtractedPreferences({})

        return ExtractedPreferences(_clean(raw))


def _render(messages: list[Message]) -> str:
    """The transcript the extractor reads.

    Assistant turns are included but labelled: the prompt tells the model
    to ignore what the assistant proposed, and it can only do that if it
    can tell the two apart. User turns alone would also lose the context
    that makes a reply like "yes, always do that" interpretable.
    """
    lines = []
    for message in messages:
        speaker = "User" if message.role == MessageRole.USER else "Assistant"
        content = (message.content or "").strip()
        if content:
            lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def _clean(raw: dict) -> dict[str, object]:
    """Keep only known fields with real values.

    Nulls are dropped rather than stored: the model returns null for "no
    evidence", and writing that through would erase a preference the user
    set in an earlier session.
    """
    cleaned: dict[str, object] = {}
    for field in _FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        if field == "captions_enabled":
            if isinstance(value, bool):
                cleaned[field] = value
            continue
        if isinstance(value, str):
            # Normalised because these are matched and rendered into
            # prompts later; "LinkedIn" and "linkedin" are the same
            # preference.
            normalised = value.strip().lower()
            if normalised and normalised not in _NULLISH:
                cleaned[field] = normalised[:32]
    return cleaned


def preferences_as_json(values: dict[str, object]) -> str:
    """Compact rendering for logs."""
    return json.dumps(values, sort_keys=True)
