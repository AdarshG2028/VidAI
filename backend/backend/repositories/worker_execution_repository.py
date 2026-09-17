"""Data access for WorkerExecution — the per-attempt audit trail."""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import WorkerExecution


class WorkerExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, execution: WorkerExecution) -> None:
        self._session.add(execution)

    async def count_for_stage(self, job_id: uuid.UUID, stage: int) -> int:
        """How many attempts this specific stage has already used — the
        retry budget is per-stage, not per-job, so a multi-stage workflow's
        earlier successful stages must not eat into a later stage's budget."""
        return await self._session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkerExecution)
            .where(WorkerExecution.job_id == job_id, WorkerExecution.stage == stage)
        )
