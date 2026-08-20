"""GraphPlanner -- the same planning turn as LLMPlanner, with the semantic
retry expressed as a real graph cycle instead of a hand-unrolled branch.

**Why this exists at all.** Not for the current behaviour, which LLMPlanner
already performs correctly in 60 lines. It exists because the next feature
-- "trim to where the person in red appears" -- needs an analyze -> assess
-> maybe-analyze-again loop before a plan can be drafted, and that is a
cycle whose iteration count depends on what the analysis found. Unrolling
that by hand the way the single regeneration is unrolled today does not
scale past one iteration. See `assess_context` in Step C for where it lands.

**What is deliberately NOT graph structure.** Infrastructure retry stays a
plain loop inside `_plan` (`_complete_with_retry`, copied verbatim from
LLMPlanner). It is homogeneous -- change nothing, try the identical request
again -- so as nodes and edges it would add three of each and express
nothing the loop doesn't. Semantic retry *is* branching (the second attempt
carries validation errors the first did not), so that becomes the cycle.
This is the same seam LLMPlanner's own docstring draws between its two
retry layers; only the second one moves.

**Bounded by an explicit counter, never by recursion_limit.** LangGraph's
recursion limit raises GraphRecursionError, which would turn "I couldn't
plan that, could you clarify?" -- a normal, friendly outcome -- into an
exception the room sees as a crash. The cap lives in `attempts` and is read
by the routing function.

No checkpointer: a planning turn is stateless by design (every call gets a
fresh PlannerContext and returns a PlannerResponse), so there is no thread
to resume and nothing to persist between turns.
"""

import logging
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.services.llm_client import LLMClient, LLMClientError
from backend.services.planner import Planner
from backend.services.planner_context import PlannerContext
from backend.services.planner_response import PLANNER_RESPONSE_SCHEMA, PlannerResponse
from backend.services.prompt_builder import Prompt, PromptBuilder
from backend.services.proposal_validator import validate_proposal

logger = logging.getLogger(__name__)

DEFAULT_INFRA_RETRY_ATTEMPTS = 3
# 1 initial attempt + 1 regeneration -- byte-for-byte the cap LLMPlanner
# hardcodes. Raising it is now a constructor argument rather than a rewrite,
# which is the point of expressing this as a cycle.
DEFAULT_MAX_SEMANTIC_ATTEMPTS = 2
# Bound on the analyze -> assess -> analyze cycle, for the same reason the
# semantic cap exists: an assessment that keeps asking for one more look is
# a real failure mode, and "give the room what we have" beats looping.
DEFAULT_MAX_ANALYSIS_ROUNDS = 2


class Analyzer(Protocol):
    """Runs content analysis on a video and returns what it found, so the
    assessment step can decide whether that is enough to plan from.

    **Not implemented yet, and deliberately so.** This is the injection
    point for semantic understanding ("trim to where the person in red
    appears"): every route to it is either a paid per-minute vision API or
    a local model that would double the deployment's memory, so the
    decision is costed and deferred rather than half-built. What exists
    today is the seam and the graph shape around it -- `analyzer=None` is
    the only configuration, and the cycle short-circuits.

    Note this breaks the planner's standing "no orchestration, no DB
    access" invariant when it does land: a planner that triggers analysis
    and waits for it is doing orchestration. That is a conscious trade for
    the feature, not an oversight -- the alternative (propose detect_scenes,
    let the room approve it, read results next turn) needs no graph at all
    and is what the product does today.

    **That alternative was tried and shipped, then dropped.** A
    find_content/remove_matches/keep_matches capability trio was built and
    verified live against Groq's vision API -- detection quality was
    genuinely good. It was removed anyway: rate limits are accounted
    separately from billing and far more restrictive than the billed cost
    suggests (measured on the free tier at ~2,900 tokens/image against an
    8,000 TPM and a 200,000 TPD budget, both metered per Groq
    *organization*, not per key), leaving roughly 2-3 short searches a day.
    Composition covered the feature correctly; it was the provider's
    economics that killed it, not this seam. Kept inert because the graph
    shape costs nothing while unused, and a cheaper or self-hosted vision
    path would revive the same design.
    """

    async def analyze(self, context: PlannerContext, query: str) -> dict: ...

_COULD_NOT_PLAN_MESSAGE = (
    "I wasn't able to put together a valid plan for that -- could you "
    "clarify what you'd like done?"
)


class PlannerState(TypedDict, total=False):
    """TypedDict rather than a Pydantic model: this repo keeps Pydantic to
    the API-schema layer, and internal domain types are plain dataclasses
    (see backend/workers/media.py's Asset for the same call)."""

    context: PlannerContext
    # Errors from the last failed validation, fed into the next prompt.
    # Absent on the first attempt, which is what makes the same `_plan`
    # node serve both the initial call and every regeneration.
    validation_feedback: list[str] | None
    # The most recent parsed response, before it is known to be terminal.
    response: PlannerResponse | None
    # Set exactly once, by whichever node decides the turn is over. Its
    # presence is what the router reads, so "are we done" is never inferred
    # from the response's own shape in two different places.
    final: PlannerResponse | None
    attempts: int
    validation_success: bool
    log_errors: list[str] | None
    # Step C. How many analyze rounds have run this turn -- the bound on
    # the assess -> analyze -> assess cycle, kept as an explicit counter
    # for the same reason `attempts` is.
    analysis_rounds: int


class GraphPlanner(Planner):
    def __init__(
        self,
        client: LLMClient,
        *,
        prompt_builder: PromptBuilder | None = None,
        infra_retry_attempts: int = DEFAULT_INFRA_RETRY_ATTEMPTS,
        max_semantic_attempts: int = DEFAULT_MAX_SEMANTIC_ATTEMPTS,
        analyzer: "Analyzer | None" = None,
        max_analysis_rounds: int = DEFAULT_MAX_ANALYSIS_ROUNDS,
    ) -> None:
        self._client = client
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._infra_retry_attempts = infra_retry_attempts
        self._max_semantic_attempts = max_semantic_attempts
        self._analyzer = analyzer
        self._max_analysis_rounds = max_analysis_rounds
        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(PlannerState)
        graph.add_node("assess_context", self._assess_context)
        graph.add_node("plan", self._plan)
        graph.add_node("validate", self._validate)
        graph.add_node("give_up", self._give_up)

        # assess_context sits upstream of plan so the future
        # assess -> analyze -> assess cycle has somewhere to attach without
        # moving anything. With no analyzer configured it is a pass-through
        # and the graph behaves exactly as it did before Step C.
        graph.add_edge(START, "assess_context")
        graph.add_conditional_edges(
            "assess_context",
            self._route_after_assess,
            # "analyze" is intentionally absent: it is added alongside the
            # node itself, so an unreachable edge cannot sit here rotting.
            {"plan": "plan"},
        )
        graph.add_edge("plan", "validate")
        graph.add_conditional_edges(
            "validate",
            self._route_after_validate,
            {"plan": "plan", "give_up": "give_up", END: END},
        )
        graph.add_edge("give_up", END)
        # Compiled once per planner instance, and get_default_planner is
        # lru_cached, so this is once per process rather than per turn.
        return graph.compile()

    async def respond(self, context: PlannerContext) -> PlannerResponse:
        """The ABC contract, unchanged: one context in, one response out.

        An LLMClientError that survives `_complete_with_retry` propagates
        out of the node and straight through `ainvoke` unwrapped (verified
        against the installed LangGraph), so callers keep seeing the same
        exception type they always have. ConversationService has no
        try/except around this and is not being asked to grow one.
        """
        final_state = await self._graph.ainvoke(
            {
                "context": context,
                "validation_feedback": None,
                "response": None,
                "final": None,
                "attempts": 0,
                "validation_success": True,
                "log_errors": None,
                "analysis_rounds": 0,
            }
        )
        self._log(
            regenerated=final_state.get("attempts", 1) > 1,
            validation_success=final_state.get("validation_success", True),
            errors=final_state.get("log_errors"),
        )
        return final_state["final"]

    async def _assess_context(self, state: PlannerState) -> dict:
        """Decide whether the planner already knows enough to draft a plan.

        Today: always yes, because no analyzer is wired. The node exists so
        the semantic loop has a home -- when an Analyzer is injected, this
        is where "the request names something I can't see in the context"
        gets decided, reading the analysis facts Step A now puts on
        VideoContext and either routing to `analyze` or falling through.

        Kept as a real node rather than a comment because the alternative
        -- adding it later -- means re-testing every path through the graph
        at the moment the risky feature lands, instead of now while the
        behaviour is provably unchanged.
        """
        return {"analysis_rounds": state.get("analysis_rounds", 0)}

    def _route_after_assess(self, state: PlannerState) -> str:
        """Always "plan" today -- there is no `analyze` node to route to, so
        pretending to branch here would be theatre.

        When an Analyzer is wired this gains exactly one more branch::

            if (
                self._analyzer is not None
                and _needs_content_analysis(state)
                and state["analysis_rounds"] < self._max_analysis_rounds
            ):
                return "analyze"

        Note the bound is the explicit `analysis_rounds` counter, not
        LangGraph's recursion_limit: exhausting that raises
        GraphRecursionError, and "I looked twice and still couldn't find
        them" should reach the room as a sentence, not a stack trace. Same
        reasoning as the semantic cap in _route_after_validate.
        """
        return "plan"

    async def _plan(self, state: PlannerState) -> dict:
        """Serves both the first attempt and every regeneration -- the only
        difference is whether validation feedback is in state."""
        feedback = state.get("validation_feedback")
        context = state["context"]
        prompt = (
            self._prompt_builder.build(context, validation_feedback=feedback)
            if feedback
            else self._prompt_builder.build(context)
        )
        raw = await self._complete_with_retry(prompt)
        return {
            "response": PlannerResponse.from_dict(raw),
            "attempts": state.get("attempts", 0) + 1,
        }

    async def _validate(self, state: PlannerState) -> dict:
        response = state["response"]

        # A message is a legitimate answer at any attempt -- including as a
        # regeneration, where it means the model chose to ask a clarifying
        # question rather than propose again. That question belongs to the
        # room; it must not be swallowed and replaced by the give-up text.
        if response.type != "proposal":
            return {"final": response, "validation_success": True}

        context = state["context"]
        validation = validate_proposal(
            response.proposal,
            context.capability_registry,
            known_video_handles=frozenset(video.handle for video in context.videos),
        )
        if validation.valid:
            return {"final": response, "validation_success": True}
        return {"validation_feedback": validation.errors}

    def _route_after_validate(self, state: PlannerState) -> str:
        if state.get("final") is not None:
            return END
        if state.get("attempts", 0) >= self._max_semantic_attempts:
            return "give_up"
        return "plan"

    async def _give_up(self, state: PlannerState) -> dict:
        """Every attempt produced an invalid proposal. Answering with a
        plain question beats handing the room a workflow that cannot run."""
        return {
            "final": PlannerResponse(type="message", message=_COULD_NOT_PLAN_MESSAGE),
            "validation_success": False,
            "log_errors": state.get("validation_feedback"),
        }

    async def _complete_with_retry(self, prompt: Prompt) -> dict:
        """Verbatim from LLMPlanner. Catches only LLMClientError, and
        re-raises the last one unwrapped once the budget is spent -- a
        provider outage is not something a clarifying question can fix."""
        last_error: LLMClientError | None = None
        for attempt in range(1, self._infra_retry_attempts + 1):
            try:
                return await self._client.complete(
                    prompt, response_schema=PLANNER_RESPONSE_SCHEMA
                )
            except LLMClientError as exc:
                last_error = exc
                logger.warning(
                    "llm_client call failed, retrying",
                    extra={"attempt": attempt, "error": str(exc)},
                )
        assert last_error is not None
        raise last_error

    def _log(
        self,
        *,
        regenerated: bool,
        validation_success: bool,
        errors: list[str] | None = None,
    ) -> None:
        extra = {
            "regeneration_used": regenerated,
            "validation_success": validation_success,
        }
        if errors:
            extra["validation_errors"] = errors
        if validation_success:
            logger.info("graph_planner turn completed", extra=extra)
        else:
            logger.warning("graph_planner could not produce a valid proposal", extra=extra)
