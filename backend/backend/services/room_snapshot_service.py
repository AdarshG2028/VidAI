"""RoomSnapshotService (Phase 8) -- assembles everything a member needs to
render a project room in one request.

Exists as a service rather than inline in the route because the snapshot
is a *composition* of five separately-owned reads (project, members,
videos, conversation, jobs) plus one real product rule -- what counts as
an export -- and that rule needs somewhere to live that a WebSocket
producer can reuse. Phase 8's `export.completed` event has to agree with
this list, or reconnecting would show a different set of exports than the
stream just announced.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Job, Message, Project, ProjectMember, Result, Video
from backend.models.enums import JobStatus
from backend.repositories.conversation_repository import ConversationRepository
from backend.repositories.message_repository import MessageRepository
from backend.repositories.project_job_repository import ProjectJobRepository
from backend.repositories.project_member_repository import ProjectMemberRepository
from backend.repositories.project_repository import ProjectNotFoundError, ProjectRepository
from backend.repositories.result_repository import ResultRepository
from backend.repositories.video_repository import VideoRepository
from backend.workers.media import Asset, PREVIEW_FLAG, previous_assets

__all__ = [
    "Export",
    "RoomSnapshot",
    "RoomSnapshotService",
    "export_artifacts",
    "final_result_by_job",
]

# How much of the transcript the snapshot carries. Bounded, unlike
# ConversationService.get_history: this is the room's opening render, and
# a months-old room should not ship its entire history on every reconnect.
# The full transcript stays one request away at GET /projects/{id}/messages.
DEFAULT_MESSAGE_LIMIT = 50


@dataclass(frozen=True)
class Export:
    job: Job
    artifacts: list[Asset]


def export_artifacts(job: Job, final_result: Result | None) -> list[Asset]:
    """What this job contributes to the room's version list -- [] if nothing.

    **The export rule, in one place on purpose.** The snapshot below and
    the progress poller's `export.completed` event both call this. A
    stream announcing a set of exports that a reconnect then disagrees
    with would be worse than emitting no event at all, and two copies of
    a four-line predicate is exactly how that happens.

    Three exclusions, only one of them naming anything:

    - **Unfinished work** is not a version.
    - **Previews are not versions.** A preview is deliberately the same
      workflow at low resolution (see ProposalConfirmationService), so
      nothing about its shape distinguishes it -- only the `_preview`
      payload flag the compiler sets, which is what this reads.
    - **Jobs that produced nothing** drop out on their own, without an
      allowlist: `video_analysis` measures a video rather than producing
      one and `dummy` produces nothing at all, so both arrive with an
      empty asset list. Filtering on what a job *left behind* rather than
      on a worker name means a future non-producing capability needs no
      change here.
    """
    if job.status != JobStatus.COMPLETED:
        return []
    if (job.payload or {}).get(PREVIEW_FLAG):
        return []
    if final_result is None:
        return []
    return previous_assets(final_result.payload)


def final_result_by_job(results: list[Result]) -> dict[uuid.UUID, Result]:
    """The last stage each job got to, keyed by job.

    The last stage rather than the last *declared* stage: a job that
    stopped early still has a meaningful newest result, and under the
    asset model that result already carries everything the earlier stages
    forwarded.
    """
    final: dict[uuid.UUID, Result] = {}
    for result in results:
        current = final.get(result.job_id)
        if current is None or result.stage > current.stage:
            final[result.job_id] = result
    return final


@dataclass(frozen=True)
class RoomSnapshot:
    project: Project
    members: list[ProjectMember]
    videos: list[Video]
    messages: list[Message]
    active_jobs: list[Job]
    # Terminal, produced nothing, and therefore in neither list above --
    # cancelled or dead-lettered. Without this a member who cancels a job
    # watches it vanish from the room on the next refresh.
    ended_jobs: list[Job]
    exports: list[Export]


class RoomSnapshotService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._members = ProjectMemberRepository(session)
        self._videos = VideoRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._project_jobs = ProjectJobRepository(session)
        self._results = ResultRepository(session)

    async def get(
        self, project_id: uuid.UUID, *, message_limit: int = DEFAULT_MESSAGE_LIMIT
    ) -> RoomSnapshot:
        project = await self._projects.get(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)

        conversation = await self._conversations.get_by_project(project_id)
        # A room with no conversation yet is ordinary, not an error: the
        # conversation is created lazily by the first message, so every
        # freshly-created project is in this state.
        messages: list[Message] = []
        if conversation is not None:
            history = await self._messages.list_by_conversation(conversation.id)
            messages = history[-message_limit:]

        return RoomSnapshot(
            project=project,
            members=await self._members.list_by_project(project_id),
            videos=await self._videos.list_by_project(project_id),
            messages=messages,
            active_jobs=await self._project_jobs.list_active_jobs(project_id),
            ended_jobs=await self._project_jobs.list_ended_without_output(project_id),
            exports=await self._exports(project_id),
        )

    async def _exports(self, project_id: uuid.UUID) -> list[Export]:
        """The room's completed jobs that left something worth keeping.

        The rule itself is `export_artifacts` above, shared with the
        socket. This only supplies it with a batch: one results query for
        every candidate rather than one per job, since the snapshot
        renders them all together.
        """
        completed = await self._project_jobs.list_completed_jobs(project_id)
        results_by_job = final_result_by_job(
            await self._results.list_by_jobs([job.id for job in completed])
        )

        exports = []
        for job in completed:
            artifacts = export_artifacts(job, results_by_job.get(job.id))
            if artifacts:
                exports.append(Export(job=job, artifacts=artifacts))
        return exports
