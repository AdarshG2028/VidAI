"""Data access for IdempotencyKey.

The key column is the primary key, so uniqueness is enforced by the database,
not by a check-then-insert in application code. The SELECT in get() is only
a fast path to avoid an unnecessary write; the IntegrityError on a duplicate
insert is what actually enforces the guarantee under concurrency.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import IdempotencyKey


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> IdempotencyKey | None:
        return await self._session.get(IdempotencyKey, key)

    def add(self, record: IdempotencyKey) -> None:
        self._session.add(record)
