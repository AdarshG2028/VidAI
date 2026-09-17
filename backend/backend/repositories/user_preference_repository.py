"""Data access for UserPreference. No business logic — that lives in services/."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import UserPreference


class UserPreferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> UserPreference | None:
        return await self._session.get(UserPreference, user_id)

    async def upsert(self, user_id: uuid.UUID, values: dict[str, object]) -> UserPreference:
        """Apply only the fields given, leaving the rest as they were.

        A partial update, not a replace: the extractor returns evidence
        from one conversation, and a user's platform preference set three
        sessions ago must survive a conversation that only mentioned
        captions.
        """
        preference = await self._session.get(UserPreference, user_id)
        if preference is None:
            preference = UserPreference(user_id=user_id)
            self._session.add(preference)
        for field, value in values.items():
            setattr(preference, field, value)
        await self._session.flush()
        return preference
