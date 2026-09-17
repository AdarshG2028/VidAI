"""Bridges job progress out of Setu and onto the room socket (Phase 8).

Setu's engine emits nothing when a job advances -- `WorkflowEngine` moves
`current_stage` and `StageProcessingService` writes the terminal status,
and neither knows a room exists. So the V1 bridge is a **server-side
watcher that polls and pushes diffs**, which is the same polling decision
Phase 7 already made for the frontend, moved server-side so N clients do
not each poll for the same answer.

The clean upgrade is backlog item 1 -- terminal lifecycle events through
the outbox -- feeding this same fan-out. When that lands only the
producer changes; the envelope, the event types and every client stay put.

**It polls only rooms that currently have a socket open.** With nobody
listening there is nothing to send, so scanning the jobs table would be
work performed for no observer. It also keeps this inert in a process
where nobody has connected, which matters more than it sounds: the test
suite runs the whole app lifespan against a database shared with a live
development stack.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.schemas.artifact import ArtifactResponse
from backend.models import Job
from backend.models.enums import JobStatus
from backend.repositories.project_job_repository import ProjectJobRepository
from backend.repositories.result_repository import ResultRepository
from backend.services.room_events import RoomEventBus
from backend.services.room_snapshot_service import export_artifacts, final_result_by_job

logger = logging.getLogger(__name__)

__all__ = ["JobProgressPoller"]


@dataclass(frozen=True)
class _JobState:
    """The parts of a job worth telling a room about."""

    status: str
    current_stage: int


class JobProgressPoller:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        events: RoomEventBus,
        *,
        interval_seconds: float = 2.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._events = events
        self._interval = interval_seconds
        # What each job looked like last tick, per room. Keyed by room and
        # not by job so the baseline can be dropped wholesale the moment a
        # room loses its last listener -- which both bounds this by
        # "rooms currently being watched" rather than by the jobs table,
        # and makes reconnecting a genuine first sighting again. A stale
        # baseline would otherwise make the first tick after a reconnect
        # re-announce an export the snapshot had just listed.
        self._seen: dict[uuid.UUID, dict[uuid.UUID, _JobState]] = {}

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:
                # Never take the API down to deliver a progress update.
                # The next tick re-reads current state from the database,
                # so a missed pass costs latency, not correctness.
                logger.exception("room progress poll failed; will retry")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def poll_once(self) -> None:
        watched = self._events.rooms()

        # Forget rooms that have gone quiet. Whoever connects next
        # re-baselines from the room snapshot, so keeping their old state
        # would only let that tick announce things the snapshot already
        # contained.
        for project_id in list(self._seen):
            if project_id not in watched:
                del self._seen[project_id]

        if not watched:
            return

        async with self._sessionmaker() as session:
            for project_id in watched:
                await self._poll_room(session, project_id)

    async def _poll_room(self, session, project_id: uuid.UUID) -> None:
        # Bounded by list_jobs_for_project's default limit, newest first.
        # A room with more than that many jobs would stop reporting
        # progress on its oldest still-running one; irrelevant at any
        # scale a single room reaches today, and the fix when it matters
        # is to fetch the room's *active* jobs here rather than its most
        # recent ones.
        jobs = await ProjectJobRepository(session).list_jobs_for_project(project_id)

        # The room's *first* tick establishes a baseline and announces
        # nothing: whoever just connected got all of this in the snapshot,
        # and replaying it would be news that is not new.
        #
        # Baselining per room rather than per job is the whole point.
        # Keying it on the job made a job that was unknown-then-finished
        # indistinguishable from a job that had finished before anyone
        # connected, so both were suppressed -- and a job that started and
        # ended *between two ticks* was therefore never reported at all.
        # Found live: a video_analysis job that lived 1.4s under a 2s
        # interval vanished silently, and a short render would have taken
        # its export.completed with it.
        baselining = project_id not in self._seen
        seen = self._seen.setdefault(project_id, {})

        for job in jobs:
            state = _JobState(status=job.status, current_stage=job.current_stage)
            previous = seen.get(job.id)
            if previous == state:
                continue
            seen[job.id] = state
            if baselining:
                continue

            self._events.publish(project_id, type="job.updated", data=_job_event(job))

            # Including a job first seen already complete: while this room
            # was being watched, it went from not existing to done, and
            # that is exactly the moment worth announcing.
            became_complete = job.status == JobStatus.COMPLETED and (
                previous is None or previous.status != JobStatus.COMPLETED
            )
            if became_complete:
                await self._announce_export(session, project_id, job)

    async def _announce_export(self, session, project_id: uuid.UUID, job: Job) -> None:
        """Emit export.completed, if this job is an export at all.

        Uses the snapshot's own predicate rather than a second copy of it:
        a client that acts on this event and then reconnects must find the
        same thing in `GET /projects/{id}`.
        """
        results = await ResultRepository(session).list_by_job(job.id)
        artifacts = export_artifacts(job, final_result_by_job(results).get(job.id))
        if not artifacts:
            return
        self._events.publish(
            project_id,
            type="export.completed",
            data={
                "job_id": str(job.id),
                "workflow": job.workflow["workflow"],
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                # Built through the API schema, deliberately: the value
                # here is the download_url format, which step 7 changed
                # and will change again at Phase 5H's presigned URLs. A
                # hand-rolled copy in this module would drift silently and
                # hand clients dead links.
                "artifacts": [
                    ArtifactResponse.from_asset(
                        kind=asset.kind, uri=asset.uri, job_id=job.id
                    ).model_dump()
                    for asset in artifacts
                ],
            },
        )


def _job_event(job: Job) -> dict:
    """A job as the room socket carries it.

    Same field names as `GET /jobs/{id}` (JobResponse), so a client
    tracking progress handles one shape whether it polled or was pushed.
    """
    workflow = job.workflow["workflow"]
    return {
        "id": str(job.id),
        "status": job.status,
        "workflow": workflow,
        "current_stage": job.current_stage,
        "total_stages": len(workflow),
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "created_at": job.created_at.isoformat(),
    }
