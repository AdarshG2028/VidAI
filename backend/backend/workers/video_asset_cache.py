"""Generic per-video asset cache (Phase 10 foundation, generalized).

Shared by every capability that can safely reuse its output across
previews/re-runs of the same *pristine* video -- currently transcribe
(the "transcript"/"srt" kinds) and detect_scenes ("scenes"). Started as
a transcript-only cache; generalized once a second capability needed the
same shape, rather than growing a second near-identical class.

Workers cannot hold a database session: WorkerRunner builds each Worker
once at process startup and reuses it for every message, while a
session is scoped per message (StageProcessingService gets the fresh
one, not the worker). So this takes the same shape as the existing
injected TranscriptionClient -- implementations open (and close) their
own short-lived session per call, the same "ambient accessor" pattern as
get_storage()/get_settings().
"""

import uuid
from typing import Any, Protocol

from backend.workers.media import Asset


class VideoAssetCache(Protocol):
    async def get(self, video_id: str, kind: str) -> Asset | None: ...

    async def put(
        self, video_id: str, kind: str, asset: Asset, data: dict[str, Any] | None = None
    ) -> None: ...


class SqlVideoAssetCache:
    """Default VideoAssetCache, backed by video_assets."""

    async def get(self, video_id: str, kind: str) -> Asset | None:
        from backend.database.session import get_sessionmaker
        from backend.repositories.video_asset_repository import VideoAssetRepository

        async with get_sessionmaker()() as session:
            row = await VideoAssetRepository(session).get(uuid.UUID(video_id), kind)
            return Asset(kind=kind, uri=row.uri) if row is not None else None

    async def put(
        self, video_id: str, kind: str, asset: Asset, data: dict[str, Any] | None = None
    ) -> None:
        from backend.database.session import get_sessionmaker
        from backend.repositories.video_asset_repository import VideoAssetRepository

        async with get_sessionmaker()() as session:
            await VideoAssetRepository(session).upsert(uuid.UUID(video_id), kind, asset.uri, data)
            await session.commit()
