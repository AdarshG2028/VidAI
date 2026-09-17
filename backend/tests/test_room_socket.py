"""The room WebSocket (Phase 8, step 8).

Everything here is a plain `def`, not `async def`, deliberately.
`TestClient.websocket_connect` is a synchronous context manager driven by
the session portal, and `receive_json()` blocks until some *other*
request in the same test pushes an event. Running that inside
pytest-asyncio's loop deadlocks.

The socket is pure fan-out, so every test has the same shape: open a
socket, make a REST call, assert on what arrives.
"""

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.websockets import WebSocketDisconnect

from backend.models import Job, ProjectJob, Result
from backend.services.job_progress_poller import JobProgressPoller
from backend.services.room_events import RoomEventBus, get_room_events
from backend.workers.media import Asset, AssetKind, assets_payload
from tests.conftest import as_user

pytestmark = pytest.mark.usefixtures("database_url")


@pytest.fixture
def room(client: TestClient, cleanup_project_ids: list):
    """A project whose owner and one joined member can both connect."""
    owner, member = uuid.uuid4(), uuid.uuid4()
    project = client.post("/projects", json={"name": "room"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    client.post(
        f"/projects/{project['id']}/members",
        json={"user_id": str(member)},
        headers=as_user(owner),
    )
    client.post(f"/projects/{project['id']}/join", headers=as_user(member))
    return project["id"], owner, member


@pytest.fixture
async def seeded_room(client: TestClient, cleanup_project_ids: list, database_url: str):
    """A room with one pending job, written straight to the database.

    Seeded rather than submitted for the reason recorded in
    tests/test_membership.py: a real submission publishes to Kafka, and a
    live worker from a running dev stack will drive the job to a terminal
    state underneath the assertions.
    """
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "poll"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))

    job_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.insert(Job).values(
                    id=job_id,
                    status="pending",
                    workflow={"workflow": ["crop", "audio"]},
                    current_stage=0,
                    payload={},
                    max_attempts=5,
                )
            )
            await conn.execute(
                sa.insert(ProjectJob).values(
                    job_id=job_id,
                    project_id=uuid.UUID(project["id"]),
                    submitted_by_user_id=owner,
                )
            )
            await conn.commit()
    finally:
        await engine.dispose()
    return project["id"], str(job_id)


async def _advance_job(database_url: str, job_id: str, *, status: str, current_stage: int):
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.update(Job)
                .where(Job.id == uuid.UUID(job_id))
                .values(
                    status=status,
                    current_stage=current_stage,
                    completed_at=sa.func.now() if status == "completed" else None,
                )
            )
            await conn.commit()
    finally:
        await engine.dispose()


async def _finish_with_asset(database_url: str, job_id: str, uri: str):
    await _advance_job(database_url, job_id, status="completed", current_stage=1)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.insert(Result).values(
                    id=uuid.uuid4(),
                    job_id=uuid.UUID(job_id),
                    worker_name="render",
                    stage=0,
                    payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=uri)]),
                )
            )
            await conn.commit()
    finally:
        await engine.dispose()


async def _seed_completed_job(database_url: str, project_id: str, uri: str) -> str:
    """A job that is already finished the first time anyone looks at it."""
    job_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.insert(Job).values(
                    id=job_id,
                    status="completed",
                    workflow={"workflow": ["crop"]},
                    current_stage=1,
                    payload={},
                    max_attempts=5,
                    completed_at=sa.func.now(),
                )
            )
            await conn.execute(
                sa.insert(ProjectJob).values(
                    job_id=job_id,
                    project_id=uuid.UUID(project_id),
                    submitted_by_user_id=uuid.uuid4(),
                )
            )
            await conn.execute(
                sa.insert(Result).values(
                    id=uuid.uuid4(),
                    job_id=job_id,
                    worker_name="crop",
                    stage=0,
                    payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=uri)]),
                )
            )
            await conn.commit()
    finally:
        await engine.dispose()
    return str(job_id)


def _drain(bus: RoomEventBus, project_id: uuid.UUID) -> list[dict]:
    """Everything queued for the room's single test subscriber."""
    subscription = next(iter(bus._rooms[project_id]))  # noqa: SLF001 - test introspection
    out = []
    while not subscription.queue.empty():
        out.append(subscription.queue.get_nowait())
    return out


def _url(project_id: str, user: uuid.UUID) -> str:
    # Identity in the query string: a browser cannot put a header on
    # new WebSocket(url), which is what the equivalence in deps.py exists
    # for.
    return f"/projects/{project_id}/ws?user_id={user}"


# --- the handshake ---------------------------------------------------------


def _refusal(client: TestClient, url: str) -> WebSocketDisconnect:
    """Connect expecting to be turned away, and return the close."""
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(url) as ws:
            ws.receive_json()
    return caught.value


def test_a_non_member_is_refused_the_socket(client: TestClient, room) -> None:
    """The socket carries the whole room — transcript, members, progress.
    Membership is checked before accept, so a stranger sees a failed
    handshake rather than a connection that opens and dies."""
    project_id, _, _ = room

    closed = _refusal(client, _url(project_id, uuid.uuid4()))

    assert closed.code == 1008


def test_a_socket_without_identity_is_refused(client: TestClient, room) -> None:
    project_id, _, _ = room

    closed = _refusal(client, f"/projects/{project_id}/ws")

    assert closed.code == 1008
    assert closed.reason == "identity required"


def test_a_nonexistent_room_is_refused_exactly_like_a_private_one(
    client: TestClient, room
) -> None:
    """Byte-identical close. Distinguishing them would tell a stranger
    whether a room id is real — the reason require_project_member returns
    404 rather than 403."""
    project_id, _, _ = room
    stranger = uuid.uuid4()

    private = _refusal(client, _url(project_id, stranger))
    missing = _refusal(client, _url(str(uuid.uuid4()), stranger))

    assert (private.code, private.reason) == (missing.code, missing.reason)
    assert private.reason == "project not found"


# --- fan-out ---------------------------------------------------------------


def test_a_message_reaches_a_member_who_did_not_send_it(client: TestClient, room) -> None:
    """The point of the whole phase: the other person's message appears
    without a refresh."""
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "hello room"},
            headers=as_user(owner),
        )

        created = ws.receive_json()

    assert created["type"] == "message.created"
    assert created["data"]["content"] == "hello room"
    assert created["data"]["sender_id"] == str(owner)


def test_the_planner_reply_is_a_second_event_not_part_of_the_first(
    client: TestClient, room
) -> None:
    """Two events from one POST, so a watching member sees the message
    land immediately rather than waiting on the planner."""
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "hello"},
            headers=as_user(owner),
        )

        created, replied = ws.receive_json(), ws.receive_json()

    assert [created["type"], replied["type"]] == ["message.created", "planner.replied"]
    assert created["data"]["role"] == "user"
    assert replied["data"]["role"] == "assistant"
    assert replied["data"]["sender_id"] is None


def test_every_connected_member_gets_the_same_event(client: TestClient, room) -> None:
    """Fan-out, not hand-off: one write reaches every socket in the room."""
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, owner)) as a:
        with client.websocket_connect(_url(project_id, member)) as b:
            client.post(
                f"/projects/{project_id}/messages",
                json={"content": "to both"},
                headers=as_user(owner),
            )

            first, second = a.receive_json(), b.receive_json()

    assert first == second
    assert first["data"]["content"] == "to both"


def test_joining_is_announced_to_the_room(client: TestClient, cleanup_project_ids) -> None:
    project_id_owner = uuid.uuid4()
    project = client.post(
        "/projects", json={"name": "room"}, headers=as_user(project_id_owner)
    ).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    invitee = uuid.uuid4()
    client.post(
        f"/projects/{project['id']}/members",
        json={"user_id": str(invitee)},
        headers=as_user(project_id_owner),
    )

    with client.websocket_connect(_url(project["id"], project_id_owner)) as ws:
        client.post(f"/projects/{project['id']}/join", headers=as_user(invitee))

        event = ws.receive_json()

    assert event["type"] == "member.joined"
    assert event["data"]["user_id"] == str(invitee)


def test_uploading_a_video_is_announced_to_the_room(
    client: TestClient, room, cleanup_project_ids
) -> None:
    """Previously only the uploader found out about a new video (from
    their own upload response); everyone else had to wait for their next
    full snapshot refetch. member here is a different user than the one
    uploading, so this proves it reaches someone who didn't cause it."""
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/videos",
            files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")},
            headers=as_user(owner),
        )

        event = ws.receive_json()

    assert event["type"] == "video.created"
    assert event["data"]["original_filename"] == "clip.mp4"


def test_renaming_a_video_is_announced_to_the_room(
    client: TestClient, room, cleanup_project_ids
) -> None:
    project_id, owner, member = room
    uploaded = client.post(
        f"/projects/{project_id}/videos",
        files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")},
        headers=as_user(owner),
    ).json()

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.patch(
            f"/projects/{project_id}/videos/{uploaded['video_id']}",
            json={"name": "Intro"},
            headers=as_user(owner),
        )

        event = ws.receive_json()

    assert event["type"] == "video.updated"
    assert event["data"]["name"] == "Intro"


def test_a_rooms_events_never_reach_another_room(client: TestClient, room, cleanup_project_ids) -> None:
    """The registry is keyed by project. Without that, every socket in the
    process would see every room's traffic."""
    project_id, owner, _ = room
    other_owner = uuid.uuid4()
    other = client.post("/projects", json={"name": "other"}, headers=as_user(other_owner)).json()
    cleanup_project_ids.append(uuid.UUID(other["id"]))

    with client.websocket_connect(_url(other["id"], other_owner)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "not yours"},
            headers=as_user(owner),
        )
        # Prove the socket is live and simply had nothing from the other
        # room: an event in its *own* room arrives, and it is the first
        # thing this socket sees.
        client.post(
            f"/projects/{other['id']}/messages",
            json={"content": "mine"},
            headers=as_user(other_owner),
        )

        event = ws.receive_json()

    assert event["data"]["content"] == "mine"


# --- seq / reconnect -------------------------------------------------------


def test_seq_advances_monotonically_within_a_room(client: TestClient, room) -> None:
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "one"},
            headers=as_user(owner),
        )
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "two"},
            headers=as_user(owner),
        )

        seqs = [ws.receive_json()["seq"] for _ in range(4)]

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 4


def test_the_snapshot_reports_the_same_counter_the_socket_advances(
    client: TestClient, room
) -> None:
    """This is what makes reconnect work: connect, snapshot, discard
    anything at or below snapshot.seq. If the two numbers came from
    different sources the comparison would be meaningless."""
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "one"},
            headers=as_user(owner),
        )
        last_seen = max(ws.receive_json()["seq"] for _ in range(2))

    snapshot = client.get(f"/projects/{project_id}", headers=as_user(owner)).json()

    assert snapshot["seq"] == last_seen


def test_seq_keeps_climbing_across_a_disconnect(client: TestClient, room) -> None:
    """A room's sequence has to outlive its subscribers. If it restarted
    when the last socket left, a reconnecting client would see numbers it
    had already used and read every reconnect as a gap."""
    project_id, owner, member = room

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "one"},
            headers=as_user(owner),
        )
        before = max(ws.receive_json()["seq"] for _ in range(2))

    # Nobody connected for this one; the counter must still advance.
    client.post(
        f"/projects/{project_id}/messages",
        json={"content": "two"},
        headers=as_user(owner),
    )

    with client.websocket_connect(_url(project_id, member)) as ws:
        client.post(
            f"/projects/{project_id}/messages",
            json={"content": "three"},
            headers=as_user(owner),
        )
        after = ws.receive_json()["seq"]

    assert after > before + 1, "events published with nobody listening did not advance seq"


# --- the bus itself --------------------------------------------------------


def test_publishing_to_an_empty_room_is_not_an_error() -> None:
    bus = RoomEventBus()
    assert bus.publish(uuid.uuid4(), type="message.created", data={}) == 1


def test_rooms_lists_only_rooms_with_subscribers() -> None:
    """What the progress poller works from — with nobody listening there
    is nothing to send, so polling that room would be work done for no
    observer."""
    bus = RoomEventBus()
    project_id = uuid.uuid4()
    assert bus.rooms() == []

    subscription = bus.subscribe(project_id)
    assert bus.rooms() == [project_id]

    bus.unsubscribe(subscription)
    assert bus.rooms() == []


def test_a_client_that_stops_reading_is_closed_rather_than_silently_trimmed() -> None:
    """A dropped event leaves a client quietly wrong until it happens to
    notice a seq gap. Closing sends it down the documented recovery path
    — reconnect, refetch the snapshot, resume."""
    bus = RoomEventBus(queue_size=2)
    project_id = uuid.uuid4()
    subscription = bus.subscribe(project_id)

    for _ in range(5):
        bus.publish(project_id, type="message.created", data={})

    assert subscription.overflowed is True
    drained = [subscription.queue.get_nowait() for _ in range(subscription.queue.qsize())]
    assert drained[-1] is not None and not isinstance(drained[-1], dict), (
        "the close sentinel should be the last thing a fallen-behind client gets"
    )


def test_the_bus_accessor_is_process_wide() -> None:
    """A registry request handlers write to and socket tasks read from
    only works if both reach the same object."""
    assert get_room_events() is get_room_events()


# --- the progress bridge ---------------------------------------------------
#
# poll_once() is driven directly rather than through run_forever's timer:
# these assert what a tick *does*, and sleeping for a real interval would
# buy nothing but flakiness.


@pytest.fixture
async def poller(database_url: str):
    """A poller on its own bus, so tests never race the app's own task."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    bus = RoomEventBus()
    yield JobProgressPoller(
        async_sessionmaker(bind=engine, expire_on_commit=False), bus
    ), bus
    await engine.dispose()


@pytest.mark.asyncio
async def test_only_rooms_with_a_listener_are_queried(poller, monkeypatch) -> None:
    """The property that keeps this cheap, and keeps it inert in a process
    where nobody has connected. Asserted in both directions, or "queried
    nothing" would pass for a poller that never queries anything."""
    progress, bus = poller
    queried: list[uuid.UUID] = []
    monkeypatch.setattr(
        "backend.services.job_progress_poller.ProjectJobRepository",
        lambda session: _RecordingRepo(queried),
    )

    await progress.poll_once()
    assert queried == []

    watched = uuid.uuid4()
    bus.subscribe(watched)
    bus.publish(uuid.uuid4(), type="message.created", data={})  # a room with nobody in it
    await progress.poll_once()

    assert queried == [watched]


@pytest.mark.asyncio
async def test_a_stage_advancing_is_pushed_to_the_room(
    poller, database_url: str, seeded_room
) -> None:
    """What replaces every client polling GET /jobs/{id} for itself."""
    progress, bus = poller
    project_id, job_id = seeded_room
    bus.subscribe(uuid.UUID(project_id))

    await progress.poll_once()  # baseline
    await _advance_job(database_url, job_id, status="running", current_stage=1)
    await progress.poll_once()

    published = _drain(bus, uuid.UUID(project_id))
    # One event, not two: the first tick only baselined, since the client
    # already had that state from the snapshot.
    assert [e["type"] for e in published] == ["job.updated"]
    assert published[-1]["data"]["current_stage"] == 1
    assert published[-1]["data"]["status"] == "running"


@pytest.mark.asyncio
async def test_an_unchanged_job_is_not_re_announced(
    poller, database_url: str, seeded_room
) -> None:
    """Diffs, not snapshots. A poller that re-sent everything every tick
    would make seq useless as a gap detector."""
    progress, bus = poller
    project_id, _ = seeded_room
    bus.subscribe(uuid.UUID(project_id))

    await progress.poll_once()
    before = bus.current_seq(uuid.UUID(project_id))
    await progress.poll_once()
    await progress.poll_once()

    assert bus.current_seq(uuid.UUID(project_id)) == before


@pytest.mark.asyncio
async def test_a_job_already_finished_when_you_connect_is_not_replayed(
    poller, database_url: str, seeded_room
) -> None:
    """The snapshot just handed that client this job. Announcing it again
    would be news that is not new."""
    progress, bus = poller
    project_id, job_id = seeded_room
    await _advance_job(database_url, job_id, status="completed", current_stage=1)
    bus.subscribe(uuid.UUID(project_id))

    await progress.poll_once()

    assert _drain(bus, uuid.UUID(project_id)) == []


@pytest.mark.asyncio
async def test_completing_announces_an_export_alongside_the_update(
    poller, database_url: str, seeded_room
) -> None:
    """export.completed has to agree with what a reconnect would list, so
    it runs the snapshot's own predicate."""
    progress, bus = poller
    project_id, job_id = seeded_room
    bus.subscribe(uuid.UUID(project_id))

    await progress.poll_once()
    await _finish_with_asset(database_url, job_id, "local://done.mp4")
    await progress.poll_once()

    published = _drain(bus, uuid.UUID(project_id))
    types = [e["type"] for e in published]
    assert types == ["job.updated", "export.completed"]
    export = published[-1]["data"]
    assert export["job_id"] == job_id
    # The download URL is built through the API schema, so the socket and
    # the snapshot can never hand out different links.
    assert export["artifacts"][0]["download_url"] == (
        f"/artifacts?uri=local%3A%2F%2Fdone.mp4&job_id={job_id}"
    )


@pytest.mark.asyncio
async def test_a_completed_job_that_produced_nothing_is_not_an_export(
    poller, database_url: str, seeded_room
) -> None:
    """Same rule as the snapshot: analysis jobs finish, but finish nothing."""
    progress, bus = poller
    project_id, job_id = seeded_room
    bus.subscribe(uuid.UUID(project_id))

    await progress.poll_once()
    await _advance_job(database_url, job_id, status="completed", current_stage=1)
    await progress.poll_once()

    types = [e["type"] for e in _drain(bus, uuid.UUID(project_id))]
    assert "export.completed" not in types


@pytest.mark.asyncio
async def test_a_job_that_starts_and_finishes_between_ticks_is_still_announced(
    poller, database_url: str, client: TestClient, cleanup_project_ids: list
) -> None:
    """Found in a live walkthrough: a video_analysis job lived 1.4s under
    a 2s poll interval and was never reported at all, because the poller
    first saw it already complete and treated that like history. A short
    render would have taken its export.completed with it — the room would
    only have learned of the export by refetching the snapshot, which is
    the polling this whole bridge exists to remove."""
    progress, bus = poller
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "fast"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    room = uuid.UUID(project["id"])
    bus.subscribe(room)

    await progress.poll_once()  # baseline: the room has no jobs at all

    # Created and finished entirely within one interval.
    job_id = await _seed_completed_job(database_url, project["id"], "local://fast.mp4")
    await progress.poll_once()

    published = _drain(bus, room)
    assert [e["type"] for e in published] == ["job.updated", "export.completed"]
    assert published[-1]["data"]["job_id"] == job_id


@pytest.mark.asyncio
async def test_reconnecting_does_not_replay_an_export_finished_while_away(
    poller, database_url: str, seeded_room
) -> None:
    """The baseline has to be dropped when a room goes quiet. Keeping it
    makes the first tick after a reconnect see a job it 'already knew',
    treat completion as a fresh transition, and re-announce an export the
    snapshot had just listed."""
    progress, bus = poller
    project_id, job_id = seeded_room
    room = uuid.UUID(project_id)

    subscription = bus.subscribe(room)
    await progress.poll_once()
    bus.unsubscribe(subscription)

    # Finishes while nobody is connected.
    await _finish_with_asset(database_url, job_id, "local://away.mp4")
    await progress.poll_once()

    bus.subscribe(room)
    await progress.poll_once()

    types = [e["type"] for e in _drain(bus, room)]
    assert types == [], f"replayed history to a reconnecting client: {types}"


@pytest.mark.asyncio
async def test_the_poller_publishes_onto_the_shared_bus(
    database_url: str, seeded_room
) -> None:
    """The one line where a wrong bus instance would leave job.updated
    reaching nobody while every other test stayed green: main.py wires the
    poller to get_room_events(), the same accessor the socket subscribes
    through."""
    project_id, job_id = seeded_room
    room = uuid.UUID(project_id)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        progress = JobProgressPoller(
            async_sessionmaker(bind=engine, expire_on_commit=False), get_room_events()
        )
        subscription = get_room_events().subscribe(room)
        try:
            await progress.poll_once()
            await _advance_job(database_url, job_id, status="running", current_stage=1)
            await progress.poll_once()

            queued = []
            while not subscription.queue.empty():
                queued.append(subscription.queue.get_nowait())
        finally:
            get_room_events().unsubscribe(subscription)
    finally:
        await engine.dispose()

    assert [e["type"] for e in queued] == ["job.updated"]


class _RecordingRepo:
    def __init__(self, log: list) -> None:
        self._log = log

    async def list_jobs_for_project(self, project_id, **_):
        self._log.append(project_id)
        return []
