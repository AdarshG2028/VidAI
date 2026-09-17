"""Data access for Message. No business logic — that lives in services/."""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Message


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, message: Message) -> None:
        self._session.add(message)

    async def list_by_conversation(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Message]:
        """Oldest first. `limit=None` (the history endpoint's use) returns
        the full conversation; a caller building the planner's bounded
        context window (`conversation_context_limit`) passes an explicit
        limit and gets the most recent `limit` messages instead."""
        query = (
            sa.select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        result = await self._session.execute(query)
        return list(reversed(result.scalars().all()))
