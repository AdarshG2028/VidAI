"""Approval collection in a real multi-member room (Phase 9a, step 2).

The policy arithmetic itself is proven exhaustively and without a database
in test_approval_policy.py. What is tested here is everything around it:
that votes are collected from the right people, that exactly one
submission can happen, and that a decision reaches the room.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import as_user

pytestmark = pytest.mark.usefixtures("database_url")


def _room(client: TestClient, cleanup_project_ids: list, members: int = 2):
    """A room with `members` active members and one pending proposal."""
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "vote"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))

    joined = []
    for _ in range(members - 1):
        member = uuid.uuid4()
        client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(member)},
            headers=as_user(owner),
        )
        client.post(f"/projects/{project['id']}/join", headers=as_user(member))
        joined.append(member)

    for content in ("hi", "crop it vertically"):
        client.post(
            f"/projects/{project['id']}/messages",
            json={"content": content},
            headers=as_user(owner),
        )
    proposal_id = client.get(
        f"/projects/{project['id']}/proposals", headers=as_user(owner)
    ).json()["proposals"][0]["id"]
    return project["id"], owner, joined, proposal_id


def _set_policy(client: TestClient, project_id: str, owner: uuid.UUID, policy: str) -> None:
    """Owner-only, via PATCH /projects/{id} -- the write path this file's
    admin tests previously had no choice but to route around with raw
    SQL, since nothing exposed the setting."""
    response = client.patch(
        f"/projects/{project_id}", json={"approval_policy": policy}, headers=as_user(owner)
    )
    assert response.status_code == 200
    assert response.json()["approval_policy"] == policy


# --- team (the default) ----------------------------------------------------


def test_a_partial_team_approval_runs_nothing(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """The property that matters most: real compute must not start until
    the room's policy is actually satisfied."""
    _, owner, _, proposal_id = _room(client, cleanup_project_ids, members=3)

    body = client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner)).json()

    assert body["job_id"] is None
    assert body["proposal"]["status"] == "pending"
    assert len(body["proposal"]["approval"]["awaiting"]) == 2


def test_the_last_approval_submits_exactly_one_job(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id, owner, members, proposal_id = _room(client, cleanup_project_ids, members=3)

    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner))
    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(members[0]))
    last = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(members[1])
    ).json()

    assert last["job_id"] is not None
    assert last["proposal"]["status"] == "submitted"
    # One job in the room, not three.
    snapshot = client.get(f"/projects/{project_id}", headers=as_user(owner)).json()
    job_ids = {j["id"] for j in snapshot["active_jobs"]} | {
        e["job_id"] for e in snapshot["exports"]
    } | {j["id"] for j in snapshot["ended_jobs"]}
    assert job_ids == {last["job_id"]}


def test_one_reject_ends_a_team_proposal_without_waiting(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """Unanimity is already impossible, so the room is handed back its
    conversation rather than made to finish voting on a dead proposal."""
    _, owner, members, proposal_id = _room(client, cleanup_project_ids, members=3)

    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner))
    body = client.post(
        f"/proposals/{proposal_id}/reject", headers=as_user(members[0])
    ).json()

    assert body["proposal"]["status"] == "rejected"
    assert body["job_id"] is None
    # members[1] never voted, and never needs to.
    assert body["proposal"]["approval"]["awaiting"] == []


def test_a_member_may_change_their_mind_while_it_is_open(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """One vote row per member, upserted. A tally would have to be
    un-counted when somebody switched."""
    _, owner, members, proposal_id = _room(client, cleanup_project_ids, members=3)

    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(members[0]))
    body = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(members[0])
    ).json()

    assert body["proposal"]["approval"]["approved_by"] == [str(members[0])]
    assert body["proposal"]["status"] == "pending"


def test_switching_to_reject_ends_it(client: TestClient, cleanup_project_ids: list) -> None:
    _, owner, members, proposal_id = _room(client, cleanup_project_ids, members=3)

    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(members[0]))
    body = client.post(
        f"/proposals/{proposal_id}/reject", headers=as_user(members[0])
    ).json()

    assert body["proposal"]["status"] == "rejected"


def test_an_outstanding_invitation_does_not_block_the_room(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """Counting invitees would make unanimity unreachable the moment
    anyone was invited — the room could never execute anything until every
    invitee got round to accepting."""
    project_id, owner, _, proposal_id = _room(client, cleanup_project_ids, members=1)
    client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(uuid.uuid4())},  # invited, never joins
        headers=as_user(owner),
    )

    body = client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner)).json()

    assert body["proposal"]["status"] == "submitted"


def test_a_stranger_cannot_vote(client: TestClient, cleanup_project_ids: list) -> None:
    _, _, _, proposal_id = _room(client, cleanup_project_ids, members=2)

    response = client.post(f"/proposals/{proposal_id}/approve", headers=as_user(uuid.uuid4()))

    assert response.status_code == 404


# --- admin ------------------------------------------------------------------


def test_under_admin_only_the_owners_vote_counts(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """Other members' votes are advisory. A room could unanimously approve
    and still be waiting on the one person the policy names."""
    project_id, owner, members, proposal_id = _room(client, cleanup_project_ids, members=3)
    _set_policy(client, project_id, owner, "admin")

    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(members[0]))
    body = client.post(
        f"/proposals/{proposal_id}/approve", headers=as_user(members[1])
    ).json()

    assert body["proposal"]["status"] == "pending"
    assert body["proposal"]["approval"]["awaiting"] == [str(owner)]
    assert body["proposal"]["approval"]["required"] == 1


def test_under_admin_the_owner_alone_submits(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id, owner, _, proposal_id = _room(client, cleanup_project_ids, members=3)
    _set_policy(client, project_id, owner, "admin")

    body = client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner)).json()

    assert body["proposal"]["status"] == "submitted"
    assert body["job_id"] is not None


def test_under_admin_a_members_rejection_does_not_kill_it(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id, owner, members, proposal_id = _room(client, cleanup_project_ids, members=3)
    _set_policy(client, project_id, owner, "admin")

    body = client.post(
        f"/proposals/{proposal_id}/reject", headers=as_user(members[0])
    ).json()

    assert body["proposal"]["status"] == "pending"


def test_switching_policy_applies_to_the_next_vote_not_retroactively(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """A policy change takes effect for whatever votes happen next; a
    proposal already sitting with partial votes is not grandfathered into
    whatever rule was active when it was created. Start under `team`, get
    one of two members in, switch to `admin`, and let the owner's vote
    alone decide it — proving the *new* rule is what's being evaluated,
    not the one active when the first vote landed."""
    project_id, owner, members, proposal_id = _room(client, cleanup_project_ids, members=2)
    client.post(f"/proposals/{proposal_id}/approve", headers=as_user(members[0]))

    _set_policy(client, project_id, owner, "admin")
    body = client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner)).json()

    assert body["proposal"]["status"] == "submitted"


# --- the room hears about it ------------------------------------------------


def test_a_proposal_is_announced_to_the_room(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """proposal.created rides the same socket as everything else, so a
    member watching sees the proposal card appear without refreshing."""
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "sock"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    client.post(
        f"/projects/{project['id']}/messages",
        json={"content": "hi"},
        headers=as_user(owner),
    )

    url = f"/projects/{project['id']}/ws?user_id={owner}"
    with client.websocket_connect(url) as ws:
        client.post(
            f"/projects/{project['id']}/messages",
            json={"content": "crop it vertically"},
            headers=as_user(owner),
        )
        events = [ws.receive_json() for _ in range(3)]

    assert [e["type"] for e in events] == [
        "message.created",
        "planner.replied",
        "proposal.created",
    ]
    assert events[-1]["data"]["status"] == "pending"


def test_a_decision_is_announced_to_the_room(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id, owner, members, proposal_id = _room(client, cleanup_project_ids, members=2)

    url = f"/projects/{project_id}/ws?user_id={members[0]}"
    with client.websocket_connect(url) as ws:
        client.post(f"/proposals/{proposal_id}/approve", headers=as_user(owner))
        event = ws.receive_json()

    assert event["type"] == "proposal.updated"
    assert event["data"]["id"] == proposal_id


# --- setting the approval policy (PATCH /projects/{id}) --------------------
#
# Until this endpoint existed, `team` — the migration default — was the
# only policy any room could ever have: nothing wrote to the column, so
# every `admin` test above had no choice but to set it with raw SQL.


def _bare_room(client: TestClient, cleanup_project_ids: list) -> tuple[str, uuid.UUID, uuid.UUID]:
    """A room with an owner and one joined member, no proposal — cheaper
    than `_room` for tests that never touch the planner."""
    owner, member = uuid.uuid4(), uuid.uuid4()
    project = client.post("/projects", json={"name": "policy"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    client.post(
        f"/projects/{project['id']}/members",
        json={"user_id": str(member)},
        headers=as_user(owner),
    )
    client.post(f"/projects/{project['id']}/join", headers=as_user(member))
    return project["id"], owner, member


def test_a_fresh_room_defaults_to_team(client: TestClient, cleanup_project_ids: list) -> None:
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "x"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))

    assert project["approval_policy"] == "team"


def test_the_owner_can_switch_to_admin(client: TestClient, cleanup_project_ids: list) -> None:
    project_id, owner, _ = _bare_room(client, cleanup_project_ids)

    response = client.patch(
        f"/projects/{project_id}", json={"approval_policy": "admin"}, headers=as_user(owner)
    )

    assert response.status_code == 200
    assert response.json()["approval_policy"] == "admin"


def test_the_change_is_visible_on_the_room_snapshot(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id, owner, _ = _bare_room(client, cleanup_project_ids)
    client.patch(
        f"/projects/{project_id}", json={"approval_policy": "admin"}, headers=as_user(owner)
    )

    snapshot = client.get(f"/projects/{project_id}", headers=as_user(owner)).json()

    assert snapshot["project"]["approval_policy"] == "admin"


def test_a_member_who_is_not_the_owner_cannot_change_the_policy(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """A member already knows the room exists, so the answer is 403, not
    404 — the same distinction require_project_owner draws for inviting."""
    project_id, _, member = _bare_room(client, cleanup_project_ids)

    response = client.patch(
        f"/projects/{project_id}", json={"approval_policy": "admin"}, headers=as_user(member)
    )

    assert response.status_code == 403


def test_a_stranger_gets_404_not_403(client: TestClient, cleanup_project_ids: list) -> None:
    project_id, _, _ = _bare_room(client, cleanup_project_ids)

    response = client.patch(
        f"/projects/{project_id}",
        json={"approval_policy": "admin"},
        headers=as_user(uuid.uuid4()),
    )

    assert response.status_code == 404


def test_an_invalid_policy_value_is_rejected(
    client: TestClient, cleanup_project_ids: list
) -> None:
    """Literal validation catches it before it ever reaches the database's
    own CHECK constraint."""
    project_id, owner, _ = _bare_room(client, cleanup_project_ids)

    response = client.patch(
        f"/projects/{project_id}",
        json={"approval_policy": "majority"},
        headers=as_user(owner),
    )

    assert response.status_code == 422


def test_an_empty_body_leaves_the_policy_unchanged(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id, owner, _ = _bare_room(client, cleanup_project_ids)

    response = client.patch(f"/projects/{project_id}", json={}, headers=as_user(owner))

    assert response.status_code == 200
    assert response.json()["approval_policy"] == "team"
