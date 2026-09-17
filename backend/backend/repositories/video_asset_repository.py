"""Data access for VideoAsset. No business logic — that lives in services/."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.video_asset import VideoAsset


class VideoAssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, video_id: uuid.UUID, kind: str) -> VideoAsset | None:
        result = await self._session.execute(
            sa.select(VideoAsset).where(
                VideoAsset.video_id == video_id, VideoAsset.kind == kind
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        video_id: uuid.UUID,
        kind: str,
        uri: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Insert this video's asset of this kind, or replace it if one
        already exists.

        ON CONFLICT rather than get-then-update: two previews of the same
        video can race to cache the same asset concurrently, and a
        separate read-then-write across two statements is exactly the
        interleaving that would raise IntegrityError under that race.
        """
        stmt = pg_insert(VideoAsset).values(
            id=uuid.uuid4(), video_id=video_id, kind=kind, uri=uri, data=data
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[VideoAsset.video_id, VideoAsset.kind],
            set_={"uri": stmt.excluded.uri, "data": stmt.excluded.data},
        )
        await self._session.execute(stmt)
