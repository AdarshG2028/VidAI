"""get_default_planner (Changelog v8) -- the Planner the running API
actually uses per request.

GraphPlanner (backed by GroqClient) when GROQ_API_KEY is configured;
StaticPlanner otherwise, so a checkout with no key still boots and the
Phase 2 conversation loop still works (also what every existing test
exercises, since no test environment sets GROQ_API_KEY). Cached like
get_settings() so GroqClient's underlying SDK client isn't rebuilt per
request -- and, now, so the planner's LangGraph is compiled once per
process rather than once per turn.
"""

from functools import lru_cache

from backend.core.config import get_settings
from backend.services.llm_client import GroqClient
from backend.services.planner import Planner, StaticPlanner


@lru_cache
def get_default_planner() -> Planner:
    settings = get_settings()
    if not settings.groq_api_key:
        return StaticPlanner()
    # Imported here, not at module scope, for the same reason
    # get_default_memory_extractor does it below: this module is imported
    # by every request path, and a top-level import would pull LangGraph
    # (and langchain-core behind it) into the no-key/StaticPlanner path --
    # including the entire test suite, none of which sets GROQ_API_KEY.
    from backend.services.graph_planner import GraphPlanner

    client = GroqClient(api_key=settings.groq_api_key, model=settings.groq_model)
    return GraphPlanner(client)


@lru_cache
def get_default_memory_extractor() -> "MemoryExtractor | None":
    """The MemoryExtractor the running API uses, or None when no LLM is
    configured.

    None rather than a stub: unlike planning, which has StaticPlanner as a
    genuine fallback that keeps the conversation loop working, there is no
    meaningful non-LLM way to tell a durable preference from a one-off
    instruction. MemoryUpdateService treats None as "nothing to learn"
    and marks the conversation processed, so a checkout without a key
    still serves the endpoint successfully instead of erroring.
    """
    from backend.services.memory_extractor import MemoryExtractor

    settings = get_settings()
    if not settings.groq_api_key:
        return None
    return MemoryExtractor(
        GroqClient(api_key=settings.groq_api_key, model=settings.groq_model)
    )
