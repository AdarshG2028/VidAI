"""Sweeps intermediate artifacts once a job has been finished a while.

Every stage stores a full copy of its output, and nothing deleted any of
it: a six-stage job on a 100MB source left roughly 600MB behind, forever.
On a single box that fills the disk long before anything else breaks.

**Not immediate.** Intermediates are what make "which stage got it wrong"
answerable, and a preview is worth re-watching, so they survive a
retention window first (settings.artifact_retention_hours). This is the
cheapest possible reclamation, deliberately: no reference counting, no
storage-wide scan, just "this job is old and finished, drop everything it
produced except the part worth keeping".

Three things are never deleted, and getting any of them wrong would
destroy user data rather than free space:

  1. **Source uploads.** videos.storage_uri is the user's own file. It
     appears in no Result, but a stage that passes video through
     unchanged (transcribe) reports the *input* URI as its output, so a
     naive sweep absolutely would delete it.
  2. **The final stage's assets.** That is the deliverable.
  3. **Anything another job still points at.** Preview and confirm jobs
     are compiled from the same proposal against the same source, so they
     share URIs by construction.
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Job, JobStatus, Result, Video
from backend.storage import get_storage

logger = logging.getLogger(__name__)

__all__ = ["ArtifactCleanupService", "CleanupOutcome"]

_TERMINAL = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.DEAD_LETTERED, JobStatus.CANCELLED)


@dataclass(frozen=True)
class CleanupOutcome:
    jobs_swept: int
    artifacts_deleted: int
    bytes_freed: int


class ArtifactCleanupService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sweep(self, *, retention_hours: float, limit: int = 100) -> CleanupOutcome:
        """Clean every finished job older than the retention window."""
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=retention_hours)
        rows = await self._session.execute(
            sa.select(Job)
            .where(
                Job.status.in_(_TERMINAL),
                Job.artifacts_cleaned_at.is_(None),
                # updated_at rather than completed_at: only success sets
                # completed_at, and a dead-lettered job's artifacts are
                # exactly the ones nobody wants kept.
                Job.updated_at < cutoff,
            )
            .order_by(Job.updated_at)
            .limit(limit)
        )
        jobs = list(rows.scalars().all())
        if not jobs:
            return CleanupOutcome(0, 0, 0)

        deleted = freed = 0
        for job in jobs:
            job_deleted, job_freed = await self._sweep_job(job)
            deleted += job_deleted
            freed += job_freed
            job.artifacts_cleaned_at = dt.datetime.now(dt.UTC)

        await self._session.commit()
        logger.info(
            "artifact cleanup swept jobs",
            extra={
                "jobs_swept": len(jobs),
                "artifacts_deleted": deleted,
                "bytes_freed": freed,
            },
        )
        return CleanupOutcome(len(jobs), deleted, freed)

    async def _sweep_job(self, job: Job) -> tuple[int, int]:
        results = list(
            (
                await self._session.execute(
                    sa.select(Result).where(Result.job_id == job.id).order_by(Result.stage)
                )
            )
            .scalars()
            .all()
        )
        if not results:
            return 0, 0

        by_stage = {result.stage: _asset_uris(result.payload) for result in results}
        keep = set(by_stage.get(max(by_stage), set()))  # the final stage's output

        candidates = {uri for uris in by_stage.values() for uri in uris} - keep
        if not candidates:
            return 0, 0

        candidates -= await self._protected(candidates, job.id)
        if not candidates:
            return 0, 0

        storage = get_storage()
        deleted = freed = 0
        for uri in candidates:
            try:
                size = storage.size(uri)
            except Exception:
                size = 0
            if storage.delete(uri):
                deleted += 1
                freed += size
        return deleted, freed

    async def _protected(self, candidates: set[str], job_id: uuid.UUID) -> set[str]:
        """URIs that must survive regardless of which job produced them."""
        uris = list(candidates)

        # A user's own upload. transcribe reports its input as its output
        # (it re-encodes nothing), so a source video really does appear in
        # a Result and would otherwise be swept.
        sources = await self._session.execute(
            sa.select(Video.storage_uri).where(Video.storage_uri.in_(uris))
        )
        protected = set(sources.scalars().all())

        # Anything another job -- a preview of the same proposal, a replay,
        # a later edit built on the same source -- still refers to.
        others = await self._session.execute(
            sa.select(Result.payload).where(Result.job_id != job_id)
        )
        for payload in others.scalars().all():
            protected |= _asset_uris(payload) & candidates
        return protected


def _asset_uris(payload: dict | None) -> set[str]:
    """Every storage URI a Result's payload refers to.

    Matched by literal shape rather than by importing media.Asset: this is
    reclamation infrastructure, and it should not stop working because a
    worker module moved.
    """
    if not isinstance(payload, dict):
        return set()
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return set()
    return {
        asset["uri"]
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("uri"), str)
    }
