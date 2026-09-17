"""Renaming a video after upload.

Split from VideoUploadService rather than folded into it: upload is "name
resolution plus five other things" (storage, Job #1, project_jobs linkage),
and this is a one-field update with one duplicate check -- sharing a class
would mean the smaller operation dragging in the bigger one's dependencies
for no reason.

Unlike upload's auto-disambiguated default name, a rename is always an
explicit choice, so a collision here is always rejected outright -- the
same rule upload already applies to an explicit name, not the one it
applies to an absent one.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Video
from backend.repositories.video_repository import (
    DuplicateVideoNameError,
    VideoRepository,
)

__all__ = ["VideoNotFoundError", "VideoRenameService"]


class VideoNotFoundError(Exception):
    """No video with this id in this project -- covers both a genuinely
    missing video and one that belongs to a different project, which are
    deliberately indistinguishable from the caller's side (matching every
    other room-scoped 404 in this API)."""

    def __init__(self, video_id: uuid.UUID) -> None:
        super().__init__(f"video {video_id} not found")
        self.video_id = video_id


class VideoRenameService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._videos = VideoRepository(session)

    async def rename(
        self, *, project_id: uuid.UUID, video_id: uuid.UUID, name: str
    ) -> Video:
        video = await self._videos.get(video_id)
        if video is None or video.project_id != project_id:
            raise VideoNotFoundError(video_id)

        existing = await self._videos.get_by_project_and_name(project_id, name)
        # Renaming a video to the name it already has is a no-op, not a
        # collision with itself.
        if existing is not None and existing.id != video_id:
            raise DuplicateVideoNameError(project_id, name)

        video.name = name
        await self._session.commit()
        return video
