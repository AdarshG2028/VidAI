"""Planner — decides what to say back in a conversation.

The interface the rest of the app depends on. `StaticPlanner` is Phase 2's
deterministic placeholder; Phase 4 added a real LLM planner behind the same
interface.

`respond` takes a `PlannerContext` and returns a `PlannerResponse` (both
typed domain objects, not `dict[str, Any]` -- Changelog v8). This is a
signature change from Phase 2's `respond(messages, preferences) -> dict`:
introducing `PlannerContext` to replace "many unrelated arguments" is only
worth doing if the planner interface itself adopts it, and typing the
output too avoids the raw-dict round trip landing right back on the return
side. `ConversationService` is the only caller and absorbs both the
context-assembly and the response-serialization work.
"""

from abc import ABC, abstractmethod

from backend.models import MessageRole
from backend.services.planner_context import PlannerContext
from backend.services.planner_response import PlannerResponse
from backend.services.proposal import Proposal, ProposalStage


class Planner(ABC):
    @abstractmethod
    async def respond(self, context: PlannerContext) -> PlannerResponse:
        """`context.conversation_history` is the conversation so far, oldest
        first, including the turn just posted. Never executes anything
        itself."""
        raise NotImplementedError


class StaticPlanner(Planner):
    """Deterministic stand-in: no LLM, same shape a real planner will return.

    First user turn in the conversation gets a clarifying question; every
    turn after that gets the same fixed proposal. The content is a
    placeholder -- only the shape is load-bearing for Phase 2.
    """

    async def respond(self, context: PlannerContext) -> PlannerResponse:
        user_turns = sum(
            1 for m in context.conversation_history if m.role == MessageRole.USER
        )
        if user_turns <= 1:
            return PlannerResponse(
                type="message", message="What would you like to do with this video?"
            )
        return PlannerResponse(
            type="proposal",
            proposal=Proposal(
                summary="Crop to 9:16 and normalize audio.",
                workflow=[
                    ProposalStage(stage="crop", params={"aspect_ratio": "9:16"}),
                    ProposalStage(stage="audio", params={"normalize": True}),
                ],
            ),
        )
