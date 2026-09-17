"""Data access for Result."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Result


class ResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: uuid.UUID, stage: int) -> Result | None:
        """Presence of this row is the idempotency check for a stage."""
        result = await self._session.execute(
            select(Result).where(Result.job_id == job_id, Result.stage == stage)
        )
        return result.scalar_one_or_none()

    async def list_by_job(self, job_id: uuid.UUID) -> list[Result]:
        """Every stage's result, in execution order — the artifact listing
        reads this to show what each stage produced, not just the final
        output."""
        result = await self._session.execute(
            select(Result).where(Result.job_id == job_id).order_by(Result.stage)
        )
        return list(result.scalars().all())

    async def get_many(
        self, job_ids: list[uuid.UUID], stage: int = 0
    ) -> dict[uuid.UUID, Result]:
        """One query for several jobs' results at a given stage.

        Used to attach each video's analysis to the planner's context; a
        per-video lookup would mean N queries on every single message.
        """
        if not job_ids:
            return {}
        rows = await self._session.execute(
            select(Result).where(Result.job_id.in_(job_ids), Result.stage == stage)
        )
        return {result.job_id: result for result in rows.scalars().all()}

    async def list_by_jobs(self, job_ids: list[uuid.UUID]) -> list[Result]:
        """Every stage's result across several jobs, in (job, stage) order.

        One query for the whole room snapshot: listing each export's
        artifacts per job would be N round trips for a view that renders
        all of them together.
        """
        if not job_ids:
            return []
        rows = await self._session.execute(
            select(Result).where(Result.job_id.in_(job_ids)).order_by(Result.job_id, Result.stage)
        )
        return list(rows.scalars().all())

    def add(self, result: Result) -> None:
        self._session.add(result)
