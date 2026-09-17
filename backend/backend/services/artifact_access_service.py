"""Who is allowed to download a stored artifact (Phase 8).

Until now `GET /artifacts?uri=...` was open to anyone who could name a
URI. The URIs are opaque and effectively unguessable, but unguessability
is not authorization: a URI appears in a room's snapshot and in every
artifact listing, so it outlives the membership of whoever saw it.

**Why authorization needs a job, not just a URI.** "Which room owns this
URI" has no single answer, and that is by construction rather than by
oversight: assets forward monotonically down a chain
(`media.forward_assets`), so a preview job and the confirm job that
follows it legitimately reference the same object, and every stage after
a `transcribe` re-reports the video it did not touch. The answerable
question is the one this service asks -- *is this job in a room you are
in, and does it actually contain this artifact* -- which is why the
download URL carries the job it was listed under.

Both halves are load-bearing. Without the membership check a stranger
downloads anything they can name; without the containment check a member
pairs a job id from their own room with a URI from a room they have
since left.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.repositories.project_job_repository import ProjectJobRepository
from backend.repositories.project_member_repository import ProjectMemberRepository
from backend.repositories.result_repository import ResultRepository
from backend.workers.media import previous_assets

__all__ = ["ArtifactAccessService"]


class ArtifactAccessService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._project_jobs = ProjectJobRepository(session)
        self._members = ProjectMemberRepository(session)
        self._results = ResultRepository(session)

    async def is_visible_to(
        self, *, uri: str, job_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        project_id = await self._project_jobs.project_for_job(job_id)
        if project_id is None:
            # A job in no room: a raw POST /jobs submission, which is
            # product-internal (two server-side call sites, never the
            # frontend). Nobody is a member of "no room", so nobody may
            # download its output -- deliberately stricter than before,
            # since "unmapped" used to mean world-readable.
            return False

        if not await self._members.is_member(project_id, user_id):
            return False

        return uri in await self._uris_of(job_id)

    async def _uris_of(self, job_id: uuid.UUID) -> set[str]:
        """Every artifact this job produced, across all stages.

        All stages, not just the final one: the per-stage listing hands
        out intermediate URIs so a client can see which step went wrong,
        and a download URL that lists must also resolve.

        `artifact_uri` is included alongside the payload assets because
        the two are populated by different mechanisms -- the column holds
        only the primary video (StageProcessingService), while the
        transcript and subtitle files exist solely inside the payload.

        Recomputed per request, including per Range request, so scrubbing
        through a long render costs one indexed read per seek. Cheap at
        this scale and correct by construction; if it ever stops being
        cheap, the fix is a cache keyed on (job_id, results count), not a
        denormalized URI column that can go stale.
        """
        uris: set[str] = set()
        for result in await self._results.list_by_job(job_id):
            if result.artifact_uri:
                uris.add(result.artifact_uri)
            uris.update(asset.uri for asset in previous_assets(result.payload))
        return uris
