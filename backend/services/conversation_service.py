"""ConversationService — the single orchestration layer for a chat turn.

Routes never touch repositories or the planner directly; this is the one
place that sequences load/create-conversation -> load-preferences ->
append-user-message -> assemble PlannerContext -> Planner.respond ->
serialize PlannerResponse -> append-assistant-message -> return. Phase 4
swaps StaticPlanner for a real GraphPlanner by passing a different Planner in
here -- the sequencing doesn't change, but this service now also owns
PlannerContext assembly (project/videos/registry/preferences/history) and
PlannerResponse serialization, since the planner interface itself became
typed (Changelog v8) rather than passing/returning plain dicts.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.models import Conversation, Message, MessageRole
from backend.models import Proposal as ProposalRow
from backend.repositories.conversation_repository import ConversationRepository
from backend.repositories.message_repository import MessageRepository
from backend.repositories.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)
from backend.repositories.proposal_repository import ProposalRepository
from backend.repositories.result_repository import ResultRepository
from backend.repositories.user_preference_repository import UserPreferenceRepository
from backend.repositories.video_repository import VideoRepository
from backend.services.capability_registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
)
from backend.services.planner import Planner, StaticPlanner
from backend.services.planner_context import (
    PlannerContext,
    build_video_contexts,
    with_edit_history,
)
from backend.services.proposal_service import proposal_event
from backend.services.room_events import get_room_events

__all__ = ["ConversationService", "PostMessageResult", "ProjectNotFoundError"]


@dataclass
class PostMessageResult:
    message_id: uuid.UUID
    response: dict[str, Any]


def _message_event(message: Message) -> dict[str, Any]:
    """A message as the room socket carries it.

    Deliberately the same field names GET /projects/{id}/messages returns
    (backend/api/schemas/conversation.py MessageResponse), so a client
    appending a streamed message to the history it already fetched is
    handling one shape, not two. Built here as a plain dict rather than
    imported from the API layer -- a service reaching up into
    api/schemas would invert the dependency for the sake of five keys.
    """
    return {
        "id": str(message.id),
        "role": message.role,
        "sender_id": str(message.sender_id) if message.sender_id else None,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        planner: Planner | None = None,
        capability_registry: CapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
    ) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._preferences = UserPreferenceRepository(session)
        self._videos = VideoRepository(session)
        self._planner = planner or StaticPlanner()
        self._capability_registry = capability_registry

    async def post_message(
        self, *, project_id: uuid.UUID, sender_id: uuid.UUID, content: str
    ) -> PostMessageResult:
        project = await self._projects.get(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)

        conversation = await self._get_or_create_conversation(project_id)

        # Loaded even though every row is empty until Phase 6 writes one --
        # keeps this sequence stable; only the preference *writer* changes
        # later, not this service.
        preferences = await self._preferences.get(sender_id)

        user_message = Message(
            conversation_id=conversation.id,
            sender_id=sender_id,
            role=MessageRole.USER,
            content=content,
        )
        self._messages.add(user_message)
        await self._session.flush()  # assigns id + created_at before the planner sees it

        history = await self._messages.list_by_conversation(
            conversation.id, limit=get_settings().conversation_context_limit
        )
        videos = await self._videos.list_by_project(project_id)
        video_contexts = build_video_contexts(videos, await self._video_analysis(videos))
        video_contexts = await with_edit_history(self._session, project_id, video_contexts)
        context = PlannerContext(
            project=project,
            conversation_history=history,
            videos=video_contexts,
            preferences=preferences,
            capability_registry=self._capability_registry,
            # Wording only -- the planner describes the room's rule so its
            # facilitation messages do not promise a render that still
            # needs votes. It never evaluates the policy (Phase 9a).
            approval_policy=project.approval_policy,
        )
        response = await self._planner.respond(context)
        response_dict = response.to_dict()

        assistant_message = Message(
            conversation_id=conversation.id,
            sender_id=None,
            role=MessageRole.ASSISTANT,
            content=json.dumps(response_dict),
        )
        self._messages.add(assistant_message)

        # A proposal becomes a row, not just a line in the transcript
        # (Phase 9a). Creation is the planner's job -- there is no manual
        # proposal endpoint -- and the author is whoever's turn produced
        # it, which becomes job ownership when the room approves it.
        proposal_row = None
        if response.type == "proposal" and response.proposal is not None:
            proposal_row = ProposalRow(
                project_id=project_id,
                created_by_user_id=sender_id,
                summary=response.proposal.summary,
                reasoning=response.proposal.reasoning,
                discussion_summary=response.proposal.discussion_summary,
                workflow={
                    "workflow": [
                        {"stage": s.stage, "video_ids": s.video_ids, "params": s.params}
                        for s in response.proposal.workflow
                    ]
                },
            )
            ProposalRepository(self._session).add(proposal_row)

        await self._session.commit()

        # Fanned out only after the commit. Emitting earlier would let a
        # client be told about a message, refetch the room snapshot, and
        # not find it -- manufacturing exactly the gap the seq counter
        # exists to report. Two events from one call, since a member
        # watching wants the other person's message the moment it lands,
        # not held back until the planner has finished thinking.
        events = get_room_events()
        events.publish(
            project_id, type="message.created", data=_message_event(user_message)
        )
        events.publish(
            project_id, type="planner.replied", data=_message_event(assistant_message)
        )
        if proposal_row is not None:
            events.publish(
                project_id, type="proposal.created", data=proposal_event(proposal_row)
            )

        return PostMessageResult(message_id=user_message.id, response=response_dict)

    async def get_history(self, project_id: uuid.UUID) -> list[Message]:
        if await self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

        conversation = await self._conversations.get_by_project(project_id)
        if conversation is None:
            return []
        # Unbounded: this is a user reading the transcript, not the planner's
        # bounded context window -- those are deliberately different limits.
        return await self._messages.list_by_conversation(conversation.id)

    async def _video_analysis(self, videos: list) -> dict[str, dict]:
        """Each video's measured metadata, keyed by video id.

        The video_analysis worker records duration, resolution and
        orientation on upload (§8), but the planner was never shown any of
        it -- so asked to "trim the last 10 seconds" it had to ask how long
        the video was, and asked to "make it vertical" it could not tell
        that it already was.

        Missing entries are normal, not errors: analysis may still be
        running, or may have failed on an unusual file. The planner simply
        gets less to work with.
        """
        job_ids = [v.latest_analysis_job_id for v in videos if v.latest_analysis_job_id]
        if not job_ids:
            return {}
        results = await ResultRepository(self._session).get_many(job_ids)
        return {
            str(video.id): results[video.latest_analysis_job_id].payload
            for video in videos
            if video.latest_analysis_job_id in results
        }

    async def _get_or_create_conversation(self, project_id: uuid.UUID) -> Conversation:
        conversation = await self._conversations.get_by_project(project_id)
        if conversation is not None:
            return conversation
        conversation = Conversation(project_id=project_id)
        self._conversations.add(conversation)
        await self._session.flush()
        return conversation
