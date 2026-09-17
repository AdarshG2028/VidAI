"""Data access for Conversation. No business logic — that lives in services/."""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Conversation


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._session.get(Conversation, conversation_id)

    def add(self, conversation: Conversation) -> None:
        self._session.add(conversation)

    async def get_by_project(self, project_id: uuid.UUID) -> Conversation | None:
        result = await self._session.execute(
            sa.select(Conversation).where(Conversation.project_id == project_id)
        )
        return result.scalar_one_or_none()
