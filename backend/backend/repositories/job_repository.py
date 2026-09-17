"""Data access for Job. No business logic — that lives in services/."""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Job, JobStatus


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, job: Job) -> None:
        self._session.add(job)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def current_status(self, job_id: uuid.UUID) -> str | None:
        """This job's status *as the database has it right now* (Phase 9b).

        Deliberately not `get()`. That is `session.get()`, which returns
        the identity-mapped object already loaded at the start of stage
        processing -- before a worker that may have run for minutes. The
        whole point of cooperative cancellation is noticing a cancel that
        arrived *during* that work, so this has to be a fresh statement.

        Safe under the connection's READ COMMITTED isolation, where every
        statement sees the latest committed rows. Under REPEATABLE READ it
        would silently return the pre-stage snapshot and cancellation
        would appear to do nothing on exactly the long renders it exists
        for.
        """
        result = await self._session.execute(select(Job.status).where(Job.id == job_id))
        return result.scalar_one_or_none()

    async def cancel(self, job_id: uuid.UUID) -> bool:
        """Mark a job cancelled unless it has already ended. True if we did.

        A conditional UPDATE rather than read-then-write, so a cancel
        racing a job's final stage cannot both "win" and leave the status
        depending on which committed second.

        `JobStatus.terminal()` is the exclusion set rather than a
        hand-rolled list -- which means a `failed` job (retryable, still
        being worked) *can* be cancelled, matching how "still active" is
        defined everywhere else in this codebase.

        `completed_at` is deliberately left null: the column means
        success, per Job's own model comment, and a cancelled job did not
        succeed.
        """
        result = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.not_in([s.value for s in JobStatus.terminal()]),
            )
            .values(status=JobStatus.CANCELLED.value)
        )
        return result.rowcount == 1
