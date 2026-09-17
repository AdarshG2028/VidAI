"""Data access for ProjectJob. No business logic — that lives in services/."""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Job, ProjectJob
from backend.models.enums import JobStatus


class ProjectJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self, *, project_id: uuid.UUID, job_id: uuid.UUID, submitted_by_user_id: uuid.UUID
    ) -> None:
        """Idempotent: a replayed submission (Setu's idempotency key returns
        the original job) must not fail here, and must not rewrite who
        owns it -- the first submitter stays the owner."""
        await self._session.execute(
            insert(ProjectJob)
            .values(
                project_id=project_id,
                job_id=job_id,
                submitted_by_user_id=submitted_by_user_id,
            )
            .on_conflict_do_nothing(index_elements=["job_id"])
        )

    async def project_for_job(self, job_id: uuid.UUID) -> uuid.UUID | None:
        """Which room this job belongs to — a primary-key lookup, since it
        is asked on every artifact fetch."""
        result = await self._session.execute(
            select(ProjectJob.project_id).where(ProjectJob.job_id == job_id)
        )
        return result.scalar_one_or_none()

    async def owner_of_job(self, job_id: uuid.UUID) -> uuid.UUID | None:
        """Who set this job running. Phase 9b authorizes cancellation
        against exactly this."""
        result = await self._session.execute(
            select(ProjectJob.submitted_by_user_id).where(ProjectJob.job_id == job_id)
        )
        return result.scalar_one_or_none()

    async def list_jobs_for_project(
        self, project_id: uuid.UUID, *, limit: int = 50
    ) -> list[Job]:
        """The room's jobs, most recent first — what the snapshot endpoint
        and the progress poller both need."""
        result = await self._session.execute(
            self._room_jobs(project_id).order_by(Job.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def list_active_jobs(self, project_id: uuid.UUID) -> list[Job]:
        """Work still outstanding in this room, oldest first.

        "Active" is `not in JobStatus.terminal()`, reusing the definition
        the engine already works to rather than inventing a second one --
        which means a `failed` job counts as active, correctly: failed is
        retryable here, and `dead_lettered` is the state that means it
        has genuinely stopped.

        Unbounded, unlike the listing above: this is what is *running*,
        and a snapshot that silently dropped an in-flight job would make
        the room look idle while it burns compute.
        """
        result = await self._session.execute(
            self._room_jobs(project_id)
            .where(Job.status.not_in([s.value for s in JobStatus.terminal()]))
            .order_by(Job.created_at)
        )
        return list(result.scalars().all())

    async def list_completed_jobs(self, project_id: uuid.UUID) -> list[Job]:
        """The room's finished jobs, most recently completed first.

        Ordered by `completed_at` rather than `created_at` because this
        feeds the derived version list (architecture doc, Phase 8: the
        room's completed export jobs *are* the version history), and jobs
        do not necessarily finish in the order they were submitted.

        Deliberately returns previews and analysis jobs too. Which of
        these count as exports is a product question, and answering it
        needs the payload -- so it belongs one layer up in
        RoomSnapshotService, not buried in a JSON predicate here.

        `id` breaks ties. `completed_at` is set Python-side per job
        (StageProcessingService) so a collision is unlikely rather than
        impossible, and an untiebroken sort lets equal timestamps come
        back in a different order on each call -- which a version list a
        user reads, and any later pagination over it, cannot tolerate.
        """
        result = await self._session.execute(
            self._room_jobs(project_id)
            .where(Job.status == JobStatus.COMPLETED.value)
            .order_by(Job.completed_at.desc(), Job.id)
        )
        return list(result.scalars().all())

    async def list_ended_without_output(self, project_id: uuid.UUID) -> list[Job]:
        """Work that stopped without producing a version (Phase 9b).

        `cancelled` and `dead_lettered` are terminal, so they are absent
        from `list_active_jobs`, and they produce no export -- which left
        them visible nowhere in the room at all. Tolerable while the only
        way to reach those states was a worker exhausting its retries;
        not tolerable once a member can press cancel and watch the job
        disappear on the next refresh, which is exactly what 9b's demo
        scenario asks to see.

        Most recently updated first: `completed_at` means success, so
        neither of these states sets it.
        """
        result = await self._session.execute(
            self._room_jobs(project_id)
            .where(
                Job.status.in_(
                    [JobStatus.CANCELLED.value, JobStatus.DEAD_LETTERED.value]
                )
            )
            .order_by(Job.updated_at.desc(), Job.id)
        )
        return list(result.scalars().all())

    def _room_jobs(self, project_id: uuid.UUID) -> Select[tuple[Job]]:
        return (
            select(Job)
            .join(ProjectJob, ProjectJob.job_id == Job.id)
            .where(ProjectJob.project_id == project_id)
        )
