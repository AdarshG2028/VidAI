"""get_default_planner: the wiring the real API route depends on to pick
StaticPlanner vs. GraphPlanner. Both get_settings and get_default_planner are
@lru_cache'd, so each test clears both caches before and after to avoid
leaking a monkeypatched setting into unrelated tests.
"""

import pytest

from backend.core.config import get_settings
from backend.services.graph_planner import GraphPlanner
from backend.services.planner import StaticPlanner
from backend.services.planner_factory import get_default_planner


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    get_default_planner.cache_clear()
    yield
    get_settings.cache_clear()
    get_default_planner.cache_clear()


def test_falls_back_to_static_planner_when_no_groq_api_key(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")

    assert isinstance(get_default_planner(), StaticPlanner)


def test_uses_graph_planner_when_groq_api_key_is_set(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    assert isinstance(get_default_planner(), GraphPlanner)
