"""Data access for Video. No business logic — that lives in services/."""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Video


class DuplicateVideoNameError(Exception):
    """A video with this name already exists in this project. Raised by
    VideoUploadService before it ever touches storage -- routes turn this
    into a 409."""

    def __init__(self, project_id: uuid.UUID, name: str) -> None:
        super().__init__(f"a video named {name!r} already exists in project {project_id}")
        self.project_id = project_id
        self.name = name


class VideoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, video: Video) -> None:
        self._session.add(video)

    async def get(self, video_id: uuid.UUID) -> Video | None:
        return await self._session.get(Video, video_id)

    async def list_by_project(self, project_id: uuid.UUID) -> list[Video]:
        result = await self._session.execute(
            sa.select(Video)
            .where(Video.project_id == project_id)
            .order_by(Video.created_at)
        )
        return list(result.scalars().all())

    async def get_by_project_and_name(self, project_id: uuid.UUID, name: str) -> Video | None:
        """Used to reject a duplicate display name at upload time -- see
        DuplicateVideoNameError. Two videos sharing a name is what makes
        the planner's video_1/video_2 handles the only thing a user can
        actually tell them apart by, which defeats the point of naming
        them at all (observed live: a planner clarifying question that
        showed the same name for both candidates)."""
        result = await self._session.execute(
            sa.select(Video).where(Video.project_id == project_id, Video.name == name)
        )
        return result.scalars().first()
