"""End-to-end tests for POST /projects/{id}/videos and GET /videos/{id}.

Runs the app's real lifespan (Kafka producer + outbox publisher), so both
Postgres and Kafka must be reachable; skips cleanly otherwise — same
pattern as test_jobs_api.py. Every video now belongs to a project (see
backend/models/video.py), so each test creates one first.
"""

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import (
    IdempotencyKey,
    Job,
    OutboxEvent,
    Project,
    Result,
    Video,
    WorkerExecution,
)
from tests.conftest import as_user

pytestmark = pytest.mark.usefixtures("database_url", "kafka_bootstrap_servers")

# `client` is a session-scoped fixture shared across every API test module —
# see its definition in conftest.py for why it can't be module-scoped here.


@pytest.fixture
async def cleanup_video_ids(database_url: str):
    created: list[uuid.UUID] = []
    yield created
    if not created:
        return
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        for video_id in created:
            job_id = await conn.scalar(
                sa.select(Video.latest_analysis_job_id).where(Video.id == video_id)
            )
            await conn.execute(sa.delete(Video).where(Video.id == video_id))
            if job_id is not None:
                await conn.execute(
                    sa.delete(WorkerExecution).where(WorkerExecution.job_id == job_id)
                )
                await conn.execute(sa.delete(Result).where(Result.job_id == job_id))
                await conn.execute(
                    sa.delete(OutboxEvent).where(OutboxEvent.aggregate_id == job_id)
                )
                await conn.execute(
                    sa.delete(IdempotencyKey).where(IdempotencyKey.job_id == job_id)
                )
                await conn.execute(sa.delete(Job).where(Job.id == job_id))
        await conn.commit()
    await engine.dispose()


@pytest.fixture
async def cleanup_project_ids(database_url: str):
    created: list[uuid.UUID] = []
    yield created
    if not created:
        return
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        # Videos in cleanup_video_ids are already gone by the time this
        # runs (fixture teardown order is LIFO); this only ever deletes an
        # empty project.
        for project_id in created:
            await conn.execute(sa.delete(Project).where(Project.id == project_id))
        await conn.commit()
    await engine.dispose()


def _create_project(client: TestClient) -> uuid.UUID:
    """Returns the project id, recording its owner in _OWNERS so later
    calls can send a membership-satisfying header without every caller
    threading the owner through."""
    owner_id = uuid.uuid4()
    response = client.post("/projects", json={}, headers=as_user(owner_id))
    assert response.status_code == 201
    body = response.json()
    project_id = uuid.UUID(body["id"])
    _OWNERS[project_id] = uuid.UUID(body["owner_id"])
    return project_id


# project id -> its owner, so _upload can send a membership-satisfying
# header without every caller having to thread the owner through.
_OWNERS: dict[uuid.UUID, uuid.UUID] = {}


def _upload(
    client: TestClient,
    project_id: uuid.UUID,
    *,
    filename: str = "clip.mp4",
    data: bytes = b"fake video bytes",
    name: str | None = None,
):
    return client.post(
        f"/projects/{project_id}/videos",
        files={"file": (filename, data, "video/mp4")},
        data={"name": name} if name is not None else None,
        headers=as_user(_OWNERS.get(project_id, project_id)),
    )


def test_upload_returns_201_with_video_and_job_id(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    response = _upload(client, project_id)
    assert response.status_code == 201
    body = response.json()
    cleanup_video_ids.append(uuid.UUID(body["video_id"]))

    assert body["project_id"] == str(project_id)
    assert body["status"] == "analyzing"
    # No name given: defaults to the uploaded file's own name.
    assert body["name"] == "clip.mp4"
    uuid.UUID(body["job_id"])  # doesn't raise


def test_upload_with_display_name_persists_and_is_readable(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    uploaded = _upload(client, project_id, filename="raw_export_final_v3.mp4", name="Intro clip")
    assert uploaded.status_code == 201
    body = uploaded.json()
    cleanup_video_ids.append(uuid.UUID(body["video_id"]))
    assert body["name"] == "Intro clip"

    detail = client.get(f"/videos/{body['video_id']}").json()
    assert detail["name"] == "Intro clip"
    assert detail["original_filename"] == "raw_export_final_v3.mp4"

    listed = client.get(f"/projects/{project_id}/videos", headers=as_user(_OWNERS[project_id])).json()["videos"]
    assert listed[0]["name"] == "Intro clip"


def test_upload_to_unknown_project_is_404(client: TestClient) -> None:
    response = _upload(client, uuid.uuid4())
    assert response.status_code == 404


def test_duplicate_name_in_the_same_project_is_409(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    """A double-click on Execute (or any retry) that reuses the same name
    must not silently create a second video -- observed live: a planner
    clarifying question that showed the same display name for two
    genuinely different videos, with no way for the user to tell them
    apart."""
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    first = _upload(client, project_id, name="vid_1")
    assert first.status_code == 201
    cleanup_video_ids.append(uuid.UUID(first.json()["video_id"]))

    second = _upload(client, project_id, name="vid_1")
    assert second.status_code == 409


def test_same_name_in_different_projects_is_allowed(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    """The constraint is scoped per project, not global -- two unrelated
    rooms naming their upload "final" is not a collision."""
    project_a = _create_project(client)
    project_b = _create_project(client)
    cleanup_project_ids.append(project_a)
    cleanup_project_ids.append(project_b)

    first = _upload(client, project_a, name="final")
    second = _upload(client, project_b, name="final")

    assert first.status_code == 201
    assert second.status_code == 201
    cleanup_video_ids.append(uuid.UUID(first.json()["video_id"]))
    cleanup_video_ids.append(uuid.UUID(second.json()["video_id"]))


def test_two_unnamed_uploads_of_the_same_filename_get_disambiguated(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    """Two phones both producing "clip.mp4" is routine, not a mistake --
    the second upload must still succeed, with a name that doesn't collide
    with the first rather than being rejected outright."""
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    first = _upload(client, project_id)
    second = _upload(client, project_id)

    assert first.status_code == 201
    assert second.status_code == 201
    cleanup_video_ids.append(uuid.UUID(first.json()["video_id"]))
    cleanup_video_ids.append(uuid.UUID(second.json()["video_id"]))
    assert first.json()["name"] == "clip.mp4"
    assert second.json()["name"] == "clip (2).mp4"


def test_disambiguation_handles_a_filename_with_no_extension(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    first = _upload(client, project_id, filename="myvideo")
    second = _upload(client, project_id, filename="myvideo")

    assert first.status_code == 201
    assert second.status_code == 201
    cleanup_video_ids.append(uuid.UUID(first.json()["video_id"]))
    cleanup_video_ids.append(uuid.UUID(second.json()["video_id"]))
    assert second.json()["name"] == "myvideo (2)"


def test_get_video_returns_analyzing_before_analysis_completes(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    uploaded = _upload(client, project_id).json()
    cleanup_video_ids.append(uuid.UUID(uploaded["video_id"]))

    response = client.get(f"/videos/{uploaded['video_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == str(project_id)
    assert body["original_filename"] == "clip.mp4"
    assert body["status"] in ("analyzing", "analyzed", "failed")
    # Whichever it is depends on whether a video_analysis worker happens to
    # be running against this broker right now — this test only proves the
    # lookup wiring, not the worker itself (see test_video_analysis_worker.py
    # for that, and the crash-recovery test pattern for a real worker run).


def test_get_unknown_video_is_404(client: TestClient) -> None:
    response = client.get(f"/videos/{uuid.uuid4()}")
    assert response.status_code == 404


def test_list_videos_returns_uploaded_videos(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    first = _upload(client, project_id, filename="first.mp4").json()
    second = _upload(client, project_id, filename="second.mp4").json()
    cleanup_video_ids.append(uuid.UUID(first["video_id"]))
    cleanup_video_ids.append(uuid.UUID(second["video_id"]))

    response = client.get(f"/projects/{project_id}/videos", headers=as_user(_OWNERS[project_id]))
    assert response.status_code == 200
    filenames = [v["original_filename"] for v in response.json()["videos"]]
    assert filenames == ["first.mp4", "second.mp4"]


def test_list_videos_for_unknown_project_is_404(client: TestClient) -> None:
    response = client.get(f"/projects/{uuid.uuid4()}/videos", headers=as_user(uuid.uuid4()))
    assert response.status_code == 404


# --- PATCH /projects/{id}/videos/{id}: renaming --------------------------


def test_rename_video_persists_the_new_name(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    video_id = _upload(client, project_id).json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.patch(
        f"/projects/{project_id}/videos/{video_id}",
        json={"name": "Final cut"},
        headers=as_user(_OWNERS[project_id]),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Final cut"

    detail = client.get(f"/videos/{video_id}").json()
    assert detail["name"] == "Final cut"


def test_rename_video_any_member_can_do_it_not_just_the_owner(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    """Renaming is a label on shared footage, not a room-level setting --
    unlike inviting members, it isn't owner-gated."""
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    video_id = _upload(client, project_id).json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    member = uuid.uuid4()
    client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(member)},
        headers=as_user(_OWNERS[project_id]),
    )
    client.post(f"/projects/{project_id}/join", headers=as_user(member))

    response = client.patch(
        f"/projects/{project_id}/videos/{video_id}",
        json={"name": "Renamed by a member"},
        headers=as_user(member),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed by a member"


def test_rename_video_to_a_name_already_taken_is_409(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    first = _upload(client, project_id, name="taken").json()["video_id"]
    second = _upload(client, project_id, filename="other.mp4").json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(first))
    cleanup_video_ids.append(uuid.UUID(second))

    response = client.patch(
        f"/projects/{project_id}/videos/{second}",
        json={"name": "taken"},
        headers=as_user(_OWNERS[project_id]),
    )
    assert response.status_code == 409


def test_rename_video_to_its_own_current_name_is_a_noop_not_a_409(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    video_id = _upload(client, project_id, name="clip").json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.patch(
        f"/projects/{project_id}/videos/{video_id}",
        json={"name": "clip"},
        headers=as_user(_OWNERS[project_id]),
    )
    assert response.status_code == 200


def test_rename_unknown_video_is_404(
    client: TestClient, cleanup_project_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    response = client.patch(
        f"/projects/{project_id}/videos/{uuid.uuid4()}",
        json={"name": "whatever"},
        headers=as_user(_OWNERS[project_id]),
    )
    assert response.status_code == 404


def test_rename_video_from_a_different_project_is_404(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    """A video id that's real but belongs to someone else's room must not
    be renamable (or even confirmable to exist) through this project."""
    project_a = _create_project(client)
    project_b = _create_project(client)
    cleanup_project_ids.append(project_a)
    cleanup_project_ids.append(project_b)
    video_id = _upload(client, project_a).json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.patch(
        f"/projects/{project_b}/videos/{video_id}",
        json={"name": "hijacked"},
        headers=as_user(_OWNERS[project_b]),
    )
    assert response.status_code == 404


def test_rename_video_by_a_stranger_is_404(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    video_id = _upload(client, project_id).json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.patch(
        f"/projects/{project_id}/videos/{video_id}",
        json={"name": "whatever"},
        headers=as_user(uuid.uuid4()),
    )
    assert response.status_code == 404


def test_rename_video_to_empty_string_is_422(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    video_id = _upload(client, project_id).json()["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.patch(
        f"/projects/{project_id}/videos/{video_id}",
        json={"name": ""},
        headers=as_user(_OWNERS[project_id]),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_upload_creates_job1_event_published_end_to_end(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list, database_url: str
) -> None:
    """The full chain: POST /projects/{id}/videos -> videos row + Job #1 ->
    outbox row -> publisher -> Kafka, mirroring test_jobs_api.py's
    equivalent for /jobs."""
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    uploaded = _upload(client, project_id).json()
    video_id = uuid.UUID(uploaded["video_id"])
    job_id = uuid.UUID(uploaded["job_id"])
    cleanup_video_ids.append(video_id)

    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        status_value = None
        for _ in range(20):
            status_value = (
                await conn.execute(
                    sa.select(OutboxEvent.status).where(OutboxEvent.aggregate_id == job_id)
                )
            ).scalar_one()
            if status_value == "published":
                break
            await asyncio.sleep(0.5)
        assert status_value == "published"
    await engine.dispose()


# --- GET /projects/{id}/videos/{id}/download ------------------------------
#
# The original upload, before any edit has run on it -- previously
# unreachable, since a fresh video_analysis job produces no assets and so
# has no sanctioned artifact to authorize a download against (see
# backend/api/routes/projects.py's download_video docstring).


def test_download_video_streams_the_uploaded_bytes(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    payload = b"pretend this is an mp4"
    uploaded = _upload(client, project_id, data=payload).json()
    video_id = uploaded["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.get(
        f"/projects/{project_id}/videos/{video_id}/download",
        headers=as_user(_OWNERS[project_id]),
    )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["accept-ranges"] == "bytes"


def test_download_video_honours_range_requests(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    payload = bytes(range(256)) * 100  # 25.6 KiB, larger than a token range
    uploaded = _upload(client, project_id, data=payload).json()
    video_id = uploaded["video_id"]
    cleanup_video_ids.append(uuid.UUID(video_id))

    response = client.get(
        f"/projects/{project_id}/videos/{video_id}/download",
        headers={**as_user(_OWNERS[project_id]), "Range": "bytes=10-19"},
    )

    assert response.status_code == 206
    assert response.content == payload[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(payload)}"


def test_download_unknown_video_is_404(client: TestClient, cleanup_project_ids: list) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)

    response = client.get(
        f"/projects/{project_id}/videos/{uuid.uuid4()}/download",
        headers=as_user(_OWNERS[project_id]),
    )

    assert response.status_code == 404


def test_download_video_via_a_different_project_is_404(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    """The video is real and downloadable -- just not through a project
    that isn't actually its own, even for a member of that other room."""
    project_a = _create_project(client)
    project_b = _create_project(client)
    cleanup_project_ids.append(project_a)
    cleanup_project_ids.append(project_b)
    uploaded = _upload(client, project_a).json()
    cleanup_video_ids.append(uuid.UUID(uploaded["video_id"]))

    response = client.get(
        f"/projects/{project_b}/videos/{uploaded['video_id']}/download",
        headers=as_user(_OWNERS[project_b]),
    )

    assert response.status_code == 404


# NOT TESTED HERE, deliberately: both streaming routes release their DB
# session before the first byte goes out (see download_video in
# backend/api/routes/projects.py), because get_session is a FastAPI
# yield-dependency whose pooled connection is otherwise held until the
# response is *fully sent* -- i.e. the whole video download. Held open by
# a browser's parallel range requests, that exhausted the pool (size 10 +
# overflow 5) and made every endpoint in the API fail with QueuePool
# timeouts, which surfaced to the user as "could not reach backend".
#
# Two attempts at a regression test for it were written and thrown away:
# both passed with the fix *and* without it, which is worse than no test.
# Starlette's TestClient drives the app through a portal and buffers each
# response fully before returning, so a request is already complete (and
# its session released) by the time the test can hold onto it -- there is
# no way to keep N streams genuinely in flight. Reproducing this needs a
# real uvicorn and real concurrent clients; it was verified that way
# instead, against the running dev server.


def test_a_stranger_cannot_download_a_rooms_video(
    client: TestClient, cleanup_project_ids: list, cleanup_video_ids: list
) -> None:
    project_id = _create_project(client)
    cleanup_project_ids.append(project_id)
    uploaded = _upload(client, project_id).json()
    cleanup_video_ids.append(uuid.UUID(uploaded["video_id"]))

    response = client.get(
        f"/projects/{project_id}/videos/{uploaded['video_id']}/download",
        headers=as_user(uuid.uuid4()),
    )

    assert response.status_code == 404
