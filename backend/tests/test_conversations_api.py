"""End-to-end tests for the Phase 2 conversation loop: POST /projects,
POST/GET /projects/{id}/messages.

API-level tests run against the real app + Postgres, same pattern as
test_videos_api.py. The context-limit test instead exercises
ConversationService directly against the `session` fixture, since it needs
to inject a spy Planner the route doesn't expose -- StaticPlanner alone
can't reveal how many messages it was actually handed.
"""

import uuid

import pytest

from tests.conftest import as_user
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.config import get_settings
from backend.models import Conversation, Message, Project
from backend.services.conversation_service import ConversationService
from backend.services.planner import Planner
from backend.services.planner_context import PlannerContext
from backend.services.planner_response import PlannerResponse

pytestmark = pytest.mark.usefixtures("database_url")

# `client` is the session-scoped fixture shared across every API test module
# — see its definition in conftest.py.


@pytest.fixture
async def cleanup_project_ids(database_url: str):
    created: list[uuid.UUID] = []
    yield created
    if not created:
        return
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        # Conversation and Message both cascade from Project, so deleting
        # the project alone is enough cleanup.
        for project_id in created:
            await conn.execute(sa.delete(Project).where(Project.id == project_id))
        await conn.commit()
    await engine.dispose()


def _create_project(client: TestClient) -> dict:
    owner_id = uuid.uuid4()
    response = client.post("/projects", json={}, headers=as_user(owner_id))
    assert response.status_code == 201
    return response.json()


def _post_message(client: TestClient, project_id: str, sender_id: str, content: str) -> dict:
    response = client.post(
        f"/projects/{project_id}/messages",
        json={"content": content},
        headers=as_user(sender_id),
    )
    assert response.status_code == 200
    return response.json()


def test_create_project_returns_201(client: TestClient, cleanup_project_ids: list) -> None:
    body = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(body["id"]))
    uuid.UUID(body["id"])  # doesn't raise
    assert body["name"] is None


def test_first_message_returns_clarifying_message(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(project["id"]))

    body = _post_message(client, project["id"], project["owner_id"], "hi")
    assert body["response"]["type"] == "message"
    uuid.UUID(body["message_id"])  # doesn't raise


def test_second_message_returns_proposal(client: TestClient, cleanup_project_ids: list) -> None:
    project = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    sender_id = project["owner_id"]

    _post_message(client, project["id"], sender_id, "hi")
    body = _post_message(client, project["id"], sender_id, "crop it vertically")

    assert body["response"]["type"] == "proposal"
    assert body["response"]["workflow"]


def test_messages_persist_in_order(client: TestClient, cleanup_project_ids: list) -> None:
    project = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    sender_id = project["owner_id"]

    contents = ["first", "second", "third"]
    for content in contents:
        _post_message(client, project["id"], sender_id, content)

    history = client.get(f"/projects/{project['id']}/messages", headers=as_user(project["owner_id"])).json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"] * 3
    assert [m["content"] for m in history if m["role"] == "user"] == contents


@pytest.mark.asyncio
async def test_conversation_is_auto_created_on_first_message(
    client: TestClient, cleanup_project_ids: list, database_url: str
) -> None:
    project = _create_project(client)
    project_id = uuid.UUID(project["id"])
    cleanup_project_ids.append(project_id)

    _post_message(client, project["id"], project["owner_id"], "hi")

    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        count = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(Conversation)
            .where(Conversation.project_id == project_id)
        )
    await engine.dispose()
    assert count == 1


def test_post_message_to_unknown_project_is_404(client: TestClient) -> None:
    response = client.post(
        f"/projects/{uuid.uuid4()}/messages",
        json={"content": "hi"},
        headers=as_user(uuid.uuid4()),
    )
    assert response.status_code == 404


def test_get_history_for_unknown_project_is_404(client: TestClient) -> None:
    response = client.get(f"/projects/{uuid.uuid4()}/messages", headers=as_user(uuid.uuid4()))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_context_limit_respected(session: AsyncSession) -> None:
    class SpyPlanner(Planner):
        def __init__(self) -> None:
            self.last_seen: list[Message] = []

        async def respond(self, context: PlannerContext) -> PlannerResponse:
            self.last_seen = context.conversation_history
            return PlannerResponse(type="message", message="ok")

    project = Project(owner_id=uuid.uuid4())
    session.add(project)
    await session.flush()

    spy = SpyPlanner()
    service = ConversationService(session, planner=spy)
    limit = get_settings().conversation_context_limit
    sender_id = uuid.uuid4()

    for i in range(limit + 5):
        await service.post_message(project_id=project.id, sender_id=sender_id, content=f"message {i}")

    assert len(spy.last_seen) == limit
    # Most recent `limit`, not the oldest -- the just-posted turn must be
    # the last element, not truncated off the end.
    assert spy.last_seen[-1].content == f"message {limit + 4}"
