"""Cooperative cancellation (Phase 9b).

The one deliberate change to Setu's core in the whole roadmap, isolated
here so the exception is reviewable on its own.

Jobs are seeded rather than submitted, and given a topic no worker
consumes: a real submission publishes to Kafka, and the running dev stack
would drive the job to a terminal state underneath these assertions (see
tests/test_membership.py).
"""

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Job, JobStatus, ProjectJob
from backend.services.stage_processing_service import StageProcessingService
from backend.workers.base import StageMessage, Worker
from tests.conftest import as_user

pytestmark = pytest.mark.usefixtures("database_url")

# Nothing in the running worker fleet subscribes to these.
WORKFLOW = ["cancel-test-a", "cancel-test-b"]


class _Ok(Worker):
    name = "cancel-test-a"

    async def process(self, message: StageMessage, previous_output: dict | None) -> dict:
        return {"processed_by": self.name}


class _Boom(Worker):
    name = "cancel-test-a"

    async def process(self, message: StageMessage, previous_output: dict | None) -> dict:
        raise RuntimeError("stage blew up")


@pytest.fixture
async def engine(database_url: str):
    eng = create_async_engine(database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
async def room_job(client: TestClient, cleanup_project_ids: list, engine):
    """A running job in a room, owned by the member who submitted it."""
    owner, member = uuid.uuid4(), uuid.uuid4()
    project = client.post("/projects", json={"name": "cancel"}, headers=as_user(owner)).json()
    cleanup_project_ids.append(uuid.UUID(project["id"]))
    client.post(
        f"/projects/{project['id']}/members",
        json={"user_id": str(member)},
        headers=as_user(owner),
    )
    client.post(f"/projects/{project['id']}/join", headers=as_user(member))

    job_id = uuid.uuid4()
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sm() as session:
        session.add(
            Job(
                id=job_id,
                status=JobStatus.RUNNING,
                workflow={"workflow": WORKFLOW},
                current_stage=0,
                payload={},
                max_attempts=3,
            )
        )
        await session.flush()
        session.add(
            ProjectJob(
                job_id=job_id,
                project_id=uuid.UUID(project["id"]),
                # The *member* submitted it, deliberately not the room
                # owner: job ownership and room ownership are different
                # things, and this is what proves the endpoint uses the
                # right one.
                submitted_by_user_id=member,
            )
        )
        await session.commit()

    yield project["id"], owner, member, str(job_id)

    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})


async def _status(engine, job_id: str) -> str:
    async with engine.connect() as conn:
        row = await conn.execute(
            sa.text("SELECT status FROM jobs WHERE id = :j"), {"j": uuid.UUID(job_id)}
        )
        return row.scalar_one()


async def _dispatched_topics(engine, job_id: str) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT topic FROM outbox_events WHERE aggregate_id = :j"),
            {"j": uuid.UUID(job_id)},
        )
        return [r[0] for r in rows]


async def _process_stage(engine, job_id: str, worker: Worker, stage: int = 0):
    """Run one stage exactly as the worker runner would."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sm() as session:
        service = StageProcessingService(session, worker)
        return await service.handle(
            StageMessage(
                job_id=uuid.UUID(job_id), stage=stage, workflow=WORKFLOW, payload={}
            )
        )


# --- authorization ---------------------------------------------------------


def test_only_the_job_owner_can_cancel(client: TestClient, room_job) -> None:
    """The room's owner is not the job's owner. Phase 9b authorizes against
    project_jobs.submitted_by_user_id, which records whoever's action
    started the work — a distinction the roadmap is explicit about."""
    _, room_owner, _, job_id = room_job

    response = client.post(f"/jobs/{job_id}/cancel", headers=as_user(room_owner))

    assert response.status_code == 403


def test_a_stranger_cannot_even_see_the_job(client: TestClient, room_job) -> None:
    """404, not 403 — someone outside the room learns nothing about
    whether that job id is real."""
    _, _, _, job_id = room_job

    response = client.post(f"/jobs/{job_id}/cancel", headers=as_user(uuid.uuid4()))

    assert response.status_code == 404


def test_cancelling_an_unmapped_job_is_a_404(client: TestClient) -> None:
    """A raw POST /jobs submission belongs to no room and has no owner to
    authorize against."""
    response = client.post(f"/jobs/{uuid.uuid4()}/cancel", headers=as_user(uuid.uuid4()))

    assert response.status_code == 404


def test_the_owner_can_cancel(client: TestClient, room_job) -> None:
    _, _, job_owner, job_id = room_job

    response = client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


# --- the engine: cooperative, not forceful ---------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_job_dispatches_no_further_stage(
    client: TestClient, room_job, engine
) -> None:
    """The heart of the feature. Asserted against the outbox rather than
    Kafka: if no event was written, no next stage can ever run."""
    _, _, job_owner, job_id = room_job
    client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    await _process_stage(engine, job_id, _Ok())

    assert await _dispatched_topics(engine, job_id) == []
    assert await _status(engine, job_id) == "cancelled"


@pytest.mark.asyncio
async def test_an_uncancelled_job_still_dispatches_its_next_stage(
    client: TestClient, room_job, engine
) -> None:
    """The control. Without this, a poller that never dispatched anything
    would pass the test above."""
    _, _, _, job_id = room_job

    await _process_stage(engine, job_id, _Ok())

    assert await _dispatched_topics(engine, job_id) == [WORKFLOW[1]]


@pytest.mark.asyncio
async def test_the_in_flight_stage_keeps_its_result(
    client: TestClient, room_job, engine
) -> None:
    """Cooperative means the running stage finishes. The user asked to
    stop, not to throw away work already paid for."""
    _, _, job_owner, job_id = room_job
    client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    await _process_stage(engine, job_id, _Ok())

    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT stage, payload FROM results WHERE job_id = :j"),
            {"j": uuid.UUID(job_id)},
        )
        results = rows.all()
    assert len(results) == 1 and results[0][0] == 0


@pytest.mark.asyncio
async def test_cancelling_during_the_last_stage_does_not_flip_back_to_completed(
    client: TestClient, room_job, engine
) -> None:
    """A cancel that lands while the final stage is running must stand.
    Writing COMPLETED would undo a deliberate action purely because the
    work happened to finish first."""
    _, _, job_owner, job_id = room_job
    client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    await _process_stage(engine, job_id, _Ok(), stage=len(WORKFLOW) - 1)

    assert await _status(engine, job_id) == "cancelled"


@pytest.mark.asyncio
async def test_a_failing_stage_does_not_overwrite_a_cancellation(
    client: TestClient, room_job, engine
) -> None:
    """Otherwise a user who cancelled would be shown `dead_lettered` and
    told the system broke, when it did exactly what they asked."""
    _, _, job_owner, job_id = room_job
    client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    for _ in range(3):  # exhaust max_attempts
        await _process_stage(engine, job_id, _Boom())

    assert await _status(engine, job_id) == "cancelled"


# --- terminal states -------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_finished_job_is_a_409_not_a_crash(
    client: TestClient, room_job, engine
) -> None:
    """The caller asked to stop something already stopped. Reporting
    success would have a UI show 'cancelling…' forever."""
    _, _, job_owner, job_id = room_job
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET status = 'completed' WHERE id = :j"),
            {"j": uuid.UUID(job_id)},
        )

    response = client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    assert response.status_code == 409


def test_cancelling_twice_is_a_409(client: TestClient, room_job) -> None:
    _, _, job_owner, job_id = room_job

    first = client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))
    second = client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    assert (first.status_code, second.status_code) == (200, 409)


@pytest.mark.asyncio
async def test_a_retrying_job_can_still_be_cancelled(
    client: TestClient, room_job, engine
) -> None:
    """`failed` is retryable, not terminal — JobStatus.terminal() omits it,
    and 'still active' means the same thing here as everywhere else."""
    _, _, job_owner, job_id = room_job
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET status = 'failed' WHERE id = :j"),
            {"j": uuid.UUID(job_id)},
        )

    response = client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    assert response.status_code == 200


# --- room visibility -------------------------------------------------------


def test_a_cancelled_job_stays_visible_in_the_room(client: TestClient, room_job) -> None:
    """Cancelled is terminal, so it leaves active_jobs, and it produces no
    export — which left it visible nowhere at all. A job a member just
    cancelled must not vanish on the next refresh."""
    project_id, room_owner, job_owner, job_id = room_job
    client.post(f"/jobs/{job_id}/cancel", headers=as_user(job_owner))

    body = client.get(f"/projects/{project_id}", headers=as_user(room_owner)).json()

    assert [j["id"] for j in body["active_jobs"]] == []
    assert [j["id"] for j in body["ended_jobs"]] == [job_id]
    assert body["ended_jobs"][0]["status"] == "cancelled"
