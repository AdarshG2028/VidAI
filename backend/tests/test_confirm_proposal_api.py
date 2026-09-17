"""Turning a proposal into a job: preview, and approval (Phase 4 → 9a).

`POST /projects/{id}/confirm-proposal` used to live here. Phase 9a removed
it: it submitted a real render on one member's say-so, which is exactly
the decision the room's approval policy now governs, and leaving it would
have made approval opt-in. Its replacement is
`POST /proposals/{id}/approve`, exercised below.

Preview kept its endpoint and is deliberately **not** governed by policy.

Runs against StaticPlanner (the app's default), which produces a real
`{"type": "proposal"}` on the second user turn.
"""

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Project
from tests.conftest import as_user

pytestmark = pytest.mark.usefixtures("database_url")


@pytest.fixture
async def cleanup_project_ids(database_url: str):
    created: list[uuid.UUID] = []
    yield created
    if not created:
        return
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
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


def _room_with_proposal(client: TestClient, cleanup_project_ids: list) -> tuple[dict, str]:
    """A solo room whose planner has produced one pending proposal."""
    project = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    _post_message(client, project["id"], project["owner_id"], "hi")
    _post_message(client, project["id"], project["owner_id"], "crop it vertically")

    listed = client.get(
        f"/projects/{project['id']}/proposals", headers=as_user(project["owner_id"])
    ).json()["proposals"]
    assert listed, "the planner's proposal was not persisted"
    return project, listed[0]["id"]


# --- the planner's proposal becomes a row ----------------------------------


def test_a_planner_proposal_is_persisted(client: TestClient, cleanup_project_ids: list) -> None:
    """Phase 4 kept proposals only in the transcript, so nothing could
    record that *this* proposal was approved by these people."""
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    body = client.get(f"/proposals/{proposal_id}", headers=as_user(project["owner_id"])).json()

    assert body["status"] == "pending"
    assert body["created_by_user_id"] == project["owner_id"]
    assert [stage["stage"] for stage in body["workflow"]] == ["crop", "audio"]


def test_a_clarifying_turn_creates_no_proposal(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """Only a proposal-shaped response becomes a row. StaticPlanner's first
    turn is a clarifying question."""
    project = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    _post_message(client, project["id"], project["owner_id"], "hi")

    listed = client.get(
        f"/projects/{project['id']}/proposals", headers=as_user(project["owner_id"])
    ).json()["proposals"]

    assert listed == []


def test_a_stranger_cannot_list_a_rooms_proposals(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project, _ = _room_with_proposal(client, cleanup_project_ids)

    response = client.get(
        f"/projects/{project['id']}/proposals", headers=as_user(uuid.uuid4())
    )

    assert response.status_code == 404


def test_a_stranger_cannot_read_a_proposal(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """404, not 403 — otherwise a stranger could probe which proposal ids
    are real."""
    _, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    assert client.get(f"/proposals/{proposal_id}", headers=as_user(uuid.uuid4())).status_code == 404


# --- approval ---------------------------------------------------------------


def test_approving_in_a_solo_room_submits_the_job(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """A one-member room satisfies `team` with a single vote: you are the
    whole team. This is what preserves the single-user experience now that
    confirm-proposal is gone."""
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    body = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(project["owner_id"])
    ).json()

    assert body["job_id"] is not None
    assert body["proposal"]["status"] == "submitted"
    assert body["proposal"]["job_id"] == body["job_id"]


def test_approval_is_idempotent(client: TestClient, cleanup_project_ids: list) -> None:
    """A second approve finds the proposal already submitted. It is a 409
    rather than a silent replay: the room's decision has been made, and
    reporting success would suggest a fresh one just happened."""
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    first = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(project["owner_id"])
    )
    second = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(project["owner_id"])
    )

    assert (first.status_code, second.status_code) == (200, 409)


def test_a_rejected_proposal_ends_and_runs_nothing(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    body = client.post(
        f"/proposals/{proposal_id}/reject", headers=as_user(project["owner_id"])
    ).json()

    assert body["proposal"]["status"] == "rejected"
    assert body["job_id"] is None and body["proposal"]["job_id"] is None


def test_the_room_keeps_talking_after_a_rejection(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """A rejected proposal does not block the room: the conversation stays
    open and the planner may propose again, as a new row. The old one is
    kept as the record of what was turned down."""
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)
    client.post(f"/proposals/{proposal_id}/reject", headers=as_user(project["owner_id"]))

    _post_message(client, project["id"], project["owner_id"], "try something else")
    listed = client.get(
        f"/projects/{project['id']}/proposals", headers=as_user(project["owner_id"])
    ).json()["proposals"]

    assert len(listed) == 2
    assert {p["status"] for p in listed} == {"pending", "rejected"}


def test_confirm_proposal_is_gone(client: TestClient, cleanup_project_ids: list) -> None:
    """It bypassed the approval policy entirely — any member could have
    spent the room's compute by calling the older endpoint."""
    project, _ = _room_with_proposal(client, cleanup_project_ids)

    response = client.post(
        f"/projects/{project['id']}/confirm-proposal", headers=as_user(project["owner_id"])
    )

    assert response.status_code == 404


# --- preview (unapproved and free) -----------------------------------------


def test_preview_needs_no_approval(client: TestClient, cleanup_project_ids: list) -> None:
    """The roadmap's open question, settled: previews are exempt from
    policy, so the iteration loop does not need a vote per tweak."""
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    response = client.post(
        f"/projects/{project['id']}/preview-proposal", headers=as_user(project["owner_id"])
    )

    assert response.status_code == 200
    assert response.json()["job_id"]
    # And the proposal is untouched — a preview is not a decision.
    body = client.get(f"/proposals/{proposal_id}", headers=as_user(project["owner_id"])).json()
    assert body["status"] == "pending"


def test_preview_and_approval_produce_different_jobs(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """Separate idempotency namespaces. One shared key would have the real
    render replay the preview's low-resolution result."""
    project, proposal_id = _room_with_proposal(client, cleanup_project_ids)

    preview = client.post(
        f"/projects/{project['id']}/preview-proposal", headers=as_user(project["owner_id"])
    ).json()
    approved = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(project["owner_id"])
    ).json()

    assert preview["job_id"] != approved["job_id"]


def test_preview_proposal_is_idempotent(client: TestClient, cleanup_project_ids: list) -> None:
    project, _ = _room_with_proposal(client, cleanup_project_ids)

    first = client.post(
        f"/projects/{project['id']}/preview-proposal", headers=as_user(project["owner_id"])
    ).json()
    second = client.post(
        f"/projects/{project['id']}/preview-proposal", headers=as_user(project["owner_id"])
    ).json()

    assert first["job_id"] == second["job_id"]
    assert second["replayed"] is True


def test_preview_with_no_proposal_yet_is_409(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project = _create_project(client)
    cleanup_project_ids.append(uuid.UUID(project["id"]))

    response = client.post(
        f"/projects/{project['id']}/preview-proposal", headers=as_user(project["owner_id"])
    )

    assert response.status_code == 409


def test_preview_for_unknown_project_is_404(client: TestClient) -> None:
    assert (
        client.post(
            f"/projects/{uuid.uuid4()}/preview-proposal", headers=as_user(uuid.uuid4())
        ).status_code
        == 404
    )
