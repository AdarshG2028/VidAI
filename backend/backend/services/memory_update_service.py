"""MemoryUpdateService — the write half of the memory loop (Phase 6).

Preferences have been *read* before every planning call since Phase 2;
nothing ever wrote them. This closes that loop.

Triggered explicitly by POST /jobs/{id}/update-memory rather than as a
side effect of polling GET /jobs/{id}: read endpoints stay read-only, and
the client decides when learning happens instead of it being an
incidental consequence of asking whether a job is done.

Two things make it safe to call carelessly, which matters because the
frontend calls it from a poll loop that may double-fire across tabs,
retries and refreshes:

  - `conversations.memory_processed_at` gates it. Once set, later calls
    return immediately without paying for another LLM round-trip.
  - A failed extraction is swallowed, not raised. The *job* succeeded;
    failing to learn from it is a soft failure and must not present to the
    user as a failed request.
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import JobStatus, Message, MessageRole
from backend.repositories.conversation_repository import ConversationRepository
from backend.repositories.job_repository import JobRepository
from backend.repositories.message_repository import MessageRepository
from backend.repositories.user_preference_repository import UserPreferenceRepository
from backend.services.memory_extractor import MemoryExtractor, preferences_as_json

logger = logging.getLogger(__name__)

__all__ = ["JobNotFoundError", "MemoryUpdateResult", "MemoryUpdateService"]

# Set into the job payload by ProposalConfirmationService, since Setu's
# Job carries no link to a conversation (it is generic infrastructure and
# knows nothing about this product). Underscore-prefixed to mark it as
# job metadata rather than worker input, matching the existing _preview
# flag. Phase 8's project_jobs table supersedes this.
CONVERSATION_ID_KEY = "_conversation_id"


class JobNotFoundError(Exception):
    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"job {job_id} not found")
        self.job_id = job_id


@dataclass(frozen=True)
class MemoryUpdateResult:
    processed: bool
    """False when this call did no work — already processed, job not
    finished, or nothing about the conversation was learnable."""

    updated_fields: list[str]


class MemoryUpdateService:
    def __init__(self, session: AsyncSession, extractor: MemoryExtractor | None) -> None:
        self._session = session
        self._extractor = extractor
        self._jobs = JobRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._preferences = UserPreferenceRepository(session)

    async def update_from_job(self, job_id: uuid.UUID) -> MemoryUpdateResult:
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)

        # Only completed jobs teach anything. A failed or still-running
        # edit says nothing durable about what the user prefers.
        if job.status != JobStatus.COMPLETED:
            return MemoryUpdateResult(processed=False, updated_fields=[])

        raw_id = (job.payload or {}).get(CONVERSATION_ID_KEY)
        conversation = await self._load_conversation(raw_id)
        if conversation is None:
            # Video-analysis jobs, jobs submitted straight through the
            # jobs API, and anything predating this field. Nothing to
            # learn from and nothing wrong.
            return MemoryUpdateResult(processed=False, updated_fields=[])

        if conversation.memory_processed_at is not None:
            return MemoryUpdateResult(processed=False, updated_fields=[])

        history = await self._messages.list_by_conversation(conversation.id)
        user_id = _speaker(history)
        if user_id is None or self._extractor is None:
            # No identifiable user, or no LLM configured (a checkout with
            # no GROQ_API_KEY). Mark it processed regardless: re-asking
            # would produce the same nothing.
            await self._mark_processed(conversation)
            return MemoryUpdateResult(processed=False, updated_fields=[])

        extracted = await self._extractor.extract(history, user_id=user_id)
        if extracted:
            await self._preferences.upsert(user_id, extracted.values)

        await self._mark_processed(conversation)
        logger.info(
            "memory updated from job",
            extra={
                "job_id": str(job_id),
                "user_id": str(user_id),
                "updated_fields": sorted(extracted.values),
                "preferences": preferences_as_json(extracted.values),
            },
        )
        return MemoryUpdateResult(
            processed=True, updated_fields=sorted(extracted.values)
        )

    async def _load_conversation(self, raw_id: object):
        if not isinstance(raw_id, str):
            return None
        try:
            conversation_id = uuid.UUID(raw_id)
        except ValueError:
            return None
        return await self._conversations.get(conversation_id)

    async def _mark_processed(self, conversation) -> None:
        conversation.memory_processed_at = dt.datetime.now(dt.UTC)
        await self._session.commit()


def _speaker(history: list[Message]) -> uuid.UUID | None:
    """Whose preferences these are.

    The most recent human sender, which is how the read path already
    identifies the user (ConversationService keys preferences on the
    posting message's sender_id). Phase 8's multi-member rooms will need a
    real answer to "whose preference is this"; until then there is exactly
    one person in a conversation.
    """
    for message in reversed(history):
        if message.role == MessageRole.USER and message.sender_id is not None:
            return message.sender_id
    return None
