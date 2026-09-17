"""Compiling a stored proposal into a Setu job (Phase 4, reworked in 9a).

**The proposal now arrives as a row, not as a message to be found.**
Phase 4 scanned the conversation backwards for the newest
`{"type": "proposal"}` blob, because there was nowhere else to keep one.
Phase 9a persists proposals, so that scan is gone -- and with it the
failure mode where one malformed historical message could break
confirmation for the whole room.

Two callers, one body: a **preview** (free, unapproved, low resolution)
and an **approval-triggered submission**. They share this code so they
provably cannot diverge in how a proposal is compiled; if they did, a
preview would stop being evidence about what the real render produces,
which is its entire purpose.

Idempotent via Setu's existing key mechanism rather than a persisted
"already submitted" flag: the key derives from the proposal's own id,
which is stable and unique, so a retry replays the same Job instead of
creating a second one -- as long as compile_workflow stays a pure function
of the proposal plus the project's video rows, which it does.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Proposal as ProposalRow
from backend.repositories.conversation_repository import ConversationRepository
from backend.repositories.project_job_repository import ProjectJobRepository
from backend.repositories.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)
from backend.repositories.proposal_repository import ProposalRepository
from backend.repositories.video_repository import VideoRepository
from backend.services.job_submission_service import (
    JobSubmissionResult,
    JobSubmissionService,
)
from backend.services.memory_update_service import CONVERSATION_ID_KEY
from backend.services.planner_context import build_video_contexts, with_edit_history
from backend.services.proposal import Proposal
from backend.services.workflow_compiler import (
    ExecutionContext,
    UnknownVideoHandleError,
    compile_workflow,
)

__all__ = ["NoPendingProposalError", "ProposalConfirmationService", "UnknownVideoHandleError"]


class NoPendingProposalError(Exception):
    """Raised when the room has no pending proposal to act on."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no pending proposal")
        self.project_id = project_id


class ProposalConfirmationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._conversations = ConversationRepository(session)
        self._videos = VideoRepository(session)
        self._proposals = ProposalRepository(session)
        self._jobs = JobSubmissionService(session)

    async def preview(
        self, project_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> JobSubmissionResult:
        """Render the room's latest pending proposal fast and low-res.

        **Not governed by the approval policy** (Phase 9a). A preview is
        cheap, reversible and deliberately low resolution, so requiring
        sign-off on every exploratory tweak would grind the very iteration
        loop previews exist to serve. Any member may preview; only a real
        render is voted on.
        """
        if await self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        proposal = await self._proposals.latest_pending(project_id)
        if proposal is None:
            raise NoPendingProposalError(project_id)
        # The requester owns a preview job, unlike an approved render whose
        # owner is the proposal's author: they triggered this one, so they
        # are who should be able to cancel it (Phase 9b).
        return await self.submit(proposal, submitted_by=user_id, preview=True)

    async def submit(
        self, proposal: ProposalRow, *, submitted_by: uuid.UUID, preview: bool
    ) -> JobSubmissionResult:
        """Compile a stored proposal and hand it to Setu.

        `submitted_by` becomes `project_jobs.submitted_by_user_id`, which
        *is* job ownership -- and for an approval-triggered submission that
        is the proposal's **author**, not whoever cast the deciding vote.
        Phase 9b authorizes cancellation directly against that column, so
        the wrong value here hands cancel rights to the wrong person.
        """
        project_id = proposal.project_id
        videos = await self._videos.list_by_project(project_id)
        contexts = build_video_contexts(videos)
        # Same resolution ConversationService used to draft this proposal's
        # prompt (see with_edit_history's docstring) -- so a handle that
        # validated when the proposal was created still resolves the same
        # way here. Each context now carries its own uri, so the map is a
        # direct read rather than a zip() that silently assumed one context
        # per video -- which stopped holding once a chained video gained a
        # second, `_original`, handle.
        contexts = await with_edit_history(self._session, project_id, contexts)
        video_uris = {context.handle: context.uri for context in contexts}
        video_db_ids = {context.handle: context.video_id for context in contexts}
        # The row keeps its stages under a "workflow" key, mirroring
        # Job.workflow, while summary and the facilitation fields live in
        # their own columns -- so the domain object is reassembled from
        # both rather than stored twice.
        workflow, payload = compile_workflow(
            Proposal.from_dict(
                {"summary": proposal.summary, "workflow": proposal.workflow["workflow"]}
            ),
            ExecutionContext(video_uris=video_uris, video_db_ids=video_db_ids, preview=preview),
        )

        # Setu's Job is generic infrastructure and carries no link back to
        # a conversation, so Phase 6's memory update has no way to find the
        # transcript that produced this job. Stamping it here is the
        # cheapest bridge; underscore-prefixed to mark it as job metadata
        # rather than worker input, like the existing _preview flag.
        # Deterministic, so it does not disturb the idempotency hash.
        conversation = await self._conversations.get_by_project(project_id)
        if conversation is not None:
            payload[CONVERSATION_ID_KEY] = str(conversation.id)

        # Separate namespaces, so previewing a proposal and then approving
        # it produce two distinct jobs rather than the approval replaying
        # the preview's low-resolution result -- which is exactly what one
        # shared key would do, since everything else about the two calls is
        # identical by construction.
        prefix = "preview-proposal" if preview else "approved-proposal"
        result = await self._jobs.submit(
            idempotency_key=f"{prefix}:{proposal.id}", workflow=workflow, payload=payload
        )
        # Binds the job to its room and records who set it running.
        # Idempotent, so a replayed submission keeps its original owner
        # rather than being reassigned to whoever asked second.
        #
        # Deliberately a *second* transaction: JobSubmissionService.submit()
        # commits the Job/OutboxEvent/IdempotencyKey trio itself, so making
        # this atomic with job creation would mean reaching into Setu's
        # submission service. A crash in the gap leaves a job with no room,
        # which is exactly the shape the project_jobs migration's backfill
        # repairs (payload._conversation_id -> project), so the recovery
        # path already exists and costs nothing to reuse.
        await ProjectJobRepository(self._session).add(
            project_id=project_id, job_id=result.job.id, submitted_by_user_id=submitted_by
        )
        await self._session.commit()
        return result
