"""The planner's two retry layers, exercised against a FakeLLMClient so no
real Groq call is needed -- infra retry (network/timeout/malformed output)
and the bounded semantic retry (validate_proposal rejection -> regenerate
with feedback -> friendly message if still invalid).

These call-count assertions are the migration's contract, kept verbatim
from the hand-unrolled LLMPlanner this replaced. During the port every one
of them ran against both implementations simultaneously (a parametrized
fixture) to prove the graph reproduced the old control flow exactly; the
old class is gone now, but the counts it pinned are still what these
assert.
"""

import pytest

from backend.services.capability_registry import DEFAULT_CAPABILITY_REGISTRY
from backend.services.graph_planner import GraphPlanner
from backend.services.llm_client import LLMClient, LLMClientError
from backend.services.planner_context import PlannerContext, VideoContext
from backend.services.prompt_builder import Prompt


@pytest.fixture
def planner_cls():
    return GraphPlanner


class FakeLLMClient(LLMClient):
    """Returns/raises each entry in `results` in sequence, one per call."""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls = 0

    async def complete(self, prompt: Prompt, *, response_schema: dict) -> dict:
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _context() -> PlannerContext:
    """Carries a real video, so `known_video_handles` is a non-empty set and
    the validator's handle checks are actually reachable. With videos=[] the
    planner passed `frozenset()` and every handle assertion was vacuous."""
    return PlannerContext(
        project=None,
        conversation_history=[],
        videos=[
            VideoContext(
                handle="video_1",
                video_id="00000000-0000-0000-0000-000000000001",
                display_name="clip.mp4",
                uri="local://clip.mp4",
            )
        ],
        preferences=None,
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
    )


VALID_MESSAGE = {"type": "message", "text": "who is this for?", "summary": None, "workflow": None}
# reasoning/discussion_summary are declared required by
# PLANNER_RESPONSE_SCHEMA but parsed with .get(), so omitting them here (as
# this fixture used to) let an implementation drop both fields with every
# test still green. Carried and asserted so a port cannot lose them.
VALID_PROPOSAL = {
    "type": "proposal",
    "text": None,
    "summary": "Run the dummy stage.",
    "workflow": [{"stage": "dummy", "video_ids": [], "params": {}}],
    "reasoning": "dummy is the safest no-op to prove the pipeline.",
    "discussion_summary": "member_1 asked for a smoke test.",
}
INVALID_PROPOSAL = {
    "type": "proposal",
    "text": None,
    "summary": "Run an unregistered stage.",
    "workflow": [{"stage": "not-a-real-stage", "video_ids": [], "params": {}}],
}
# Structurally fine and a real capability -- rejected only because the
# handle does not exist in the room. Exercises the known_video_handles path
# specifically, which nothing did while _context() carried no videos.
UNKNOWN_HANDLE_PROPOSAL = {
    "type": "proposal",
    "text": None,
    "summary": "Crop a video that isn't in this room.",
    "workflow": [{"stage": "crop", "video_ids": ["video_9"], "params": {"aspect_ratio": "9:16"}}],
}


@pytest.mark.asyncio
async def test_message_response_passes_through_with_no_retry(planner_cls) -> None:
    client = FakeLLMClient([VALID_MESSAGE])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.type == "message"
    assert response.message == "who is this for?"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_valid_proposal_passes_through_with_no_retry(planner_cls) -> None:
    client = FakeLLMClient([VALID_PROPOSAL])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.type == "proposal"
    assert response.proposal.workflow[0].stage == "dummy"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_facilitation_fields_survive_onto_the_proposal(planner_cls) -> None:
    """reasoning/discussion_summary are what the room reads to understand
    *why* a workflow was proposed (Phase 9a). They reach the Proposal via
    .get() defaults, so nothing structural would complain if an
    implementation silently stopped carrying them."""
    client = FakeLLMClient([VALID_PROPOSAL])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.proposal.reasoning == VALID_PROPOSAL["reasoning"]
    assert response.proposal.discussion_summary == VALID_PROPOSAL["discussion_summary"]


@pytest.mark.asyncio
async def test_invalid_proposal_regenerates_once_and_succeeds(planner_cls) -> None:
    client = FakeLLMClient([INVALID_PROPOSAL, VALID_PROPOSAL])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.type == "proposal"
    assert response.proposal.workflow[0].stage == "dummy"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_regeneration_returning_a_message_is_returned_to_the_user(planner_cls) -> None:
    """The branch with no coverage before this: when the regenerated turn
    asks a clarifying question instead of proposing again, that question is
    a legitimate answer and must reach the room -- not be swallowed and
    replaced by the generic "I couldn't plan that" fallback."""
    client = FakeLLMClient([INVALID_PROPOSAL, VALID_MESSAGE])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.type == "message"
    assert response.message == "who is this for?"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_unknown_video_handle_is_rejected_and_regenerates(planner_cls) -> None:
    """Reachable only because _context() now carries a real video."""
    client = FakeLLMClient([UNKNOWN_HANDLE_PROPOSAL, VALID_PROPOSAL])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.type == "proposal"
    assert response.proposal.workflow[0].stage == "dummy"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_invalid_proposal_twice_returns_friendly_clarification_not_a_third_attempt(planner_cls) -> None:
    client = FakeLLMClient([INVALID_PROPOSAL, INVALID_PROPOSAL])
    planner = planner_cls(client)

    response = await planner.respond(_context())

    assert response.type == "message"
    assert client.calls == 2  # exactly one regeneration, never a third attempt


@pytest.mark.asyncio
async def test_infra_failure_retries_then_succeeds(planner_cls) -> None:
    client = FakeLLMClient([LLMClientError("timeout"), LLMClientError("timeout"), VALID_MESSAGE])
    planner = planner_cls(client, infra_retry_attempts=3)

    response = await planner.respond(_context())

    assert response.type == "message"
    assert client.calls == 3


@pytest.mark.asyncio
async def test_infra_failure_exhausts_retries_and_raises(planner_cls) -> None:
    client = FakeLLMClient(
        [LLMClientError("timeout"), LLMClientError("timeout"), LLMClientError("timeout")]
    )
    planner = planner_cls(client, infra_retry_attempts=3)

    with pytest.raises(LLMClientError):
        await planner.respond(_context())

    assert client.calls == 3


# --- GraphPlanner-specific: the seam Step C exists to create ------------
#
# They assert the shape the semantic loop will attach to, not any
# semantic behaviour -- there is none yet, on purpose.


@pytest.mark.asyncio
async def test_assess_context_is_in_the_graph_and_is_a_pass_through() -> None:
    """The node exists now so the analyze cycle can be added later without
    re-testing every path at the moment the risky feature lands."""
    graph_nodes = GraphPlanner(FakeLLMClient([]))._graph.get_graph().nodes

    assert "assess_context" in graph_nodes
    assert "plan" in graph_nodes


@pytest.mark.asyncio
async def test_an_injected_analyzer_does_not_change_the_turn_yet() -> None:
    """The injection point accepts an analyzer, but nothing routes to it --
    so configuring one must not alter a normal planning turn. This is what
    makes shipping the seam safe ahead of the feature."""

    class NeverCalledAnalyzer:
        async def analyze(self, context, query):  # pragma: no cover
            raise AssertionError("no analyze edge exists yet")

    client = FakeLLMClient([VALID_PROPOSAL])
    planner = GraphPlanner(client, analyzer=NeverCalledAnalyzer())

    response = await planner.respond(_context())

    assert response.type == "proposal"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_semantic_attempt_cap_is_tunable_rather_than_hardcoded() -> None:
    """The one genuine behavioural gain of the migration: the regeneration
    bound is a constructor argument, not an unrolled branch. Default stays
    2 so today's behaviour is unchanged."""
    client = FakeLLMClient([INVALID_PROPOSAL, INVALID_PROPOSAL, VALID_PROPOSAL])
    planner = GraphPlanner(client, max_semantic_attempts=3)

    response = await planner.respond(_context())

    assert response.type == "proposal"
    assert client.calls == 3
