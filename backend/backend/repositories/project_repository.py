"""Data access for Project. No business logic — that lives in services/."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Project


class ProjectNotFoundError(Exception):
    """No project with this id. Shared by every service that requires a
    project to exist before doing anything else (ConversationService,
    VideoUploadService, ...) -- routes turn this into a 404."""


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, project: Project) -> None:
        self._session.add(project)

    async def get(self, project_id: uuid.UUID) -> Project | None:
        return await self._session.get(Project, project_id)
