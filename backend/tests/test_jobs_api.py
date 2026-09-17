"""End-to-end tests for POST /jobs and GET /jobs/{id}.

Runs the app's real lifespan (Kafka producer + outbox publisher), so both
Postgres and Kafka must be reachable; skips cleanly otherwise.
"""

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import IdempotencyKey, Job, OutboxEvent

pytestmark = pytest.mark.usefixtures("database_url", "kafka_bootstrap_servers")

# `client` is a session-scoped fixture shared across every API test module —
# see its definition in conftest.py for why it can't be module-scoped here.


@pytest.fixture
async def cleanup_job_ids(database_url: str):
    """Deletes any jobs created during the test, by id, after it finishes.

    Uses its own engine rather than the app's cached get_engine(): the app's
    outbox publisher keeps polling on TestClient's own event loop (a
    separate portal thread) for as long as the client fixture's `with` block
    is open, so sharing that engine's pool with this fixture's different
    event loop causes cross-loop contention on the same pool — hung tests.
    NullPool means no pooled connection is left needing cleanup after this
    fixture's own short-lived loop closes.
    """
    created: list[uuid.UUID] = []
    yield created
    if not created:
        return
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        for job_id in created:
            await conn.execute(sa.delete(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
            await conn.execute(sa.delete(IdempotencyKey).where(IdempotencyKey.job_id == job_id))
            await conn.execute(sa.delete(Job).where(Job.id == job_id))
        await conn.commit()
    await engine.dispose()


def _key() -> str:
    return f"test-{uuid.uuid4()}"


def test_create_job_returns_201(client: TestClient, cleanup_job_ids: list) -> None:
    response = client.post(
        "/jobs",
        json={"workflow": ["frame_extraction"], "payload": {"a": 1}},
        headers={"Idempotency-Key": _key()},
    )
    assert response.status_code == 201
    body = response.json()
    cleanup_job_ids.append(uuid.UUID(body["id"]))

    assert body["status"] == "pending"
    assert body["workflow"] == ["frame_extraction"]


def test_missing_idempotency_key_is_rejected(client: TestClient) -> None:
    response = client.post("/jobs", json={"workflow": ["frame_extraction"]})
    assert response.status_code == 422


def test_repeated_key_returns_same_job(client: TestClient, cleanup_job_ids: list) -> None:
    key = _key()
    body = {"workflow": ["frame_extraction"], "payload": {}}

    first = client.post("/jobs", json=body, headers={"Idempotency-Key": key})
    second = client.post("/jobs", json=body, headers={"Idempotency-Key": key})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    cleanup_job_ids.append(uuid.UUID(first.json()["id"]))


def test_repeated_key_different_body_is_conflict(client: TestClient, cleanup_job_ids: list) -> None:
    key = _key()

    first = client.post(
        "/jobs",
        json={"workflow": ["frame_extraction"], "payload": {"a": 1}},
        headers={"Idempotency-Key": key},
    )
    second = client.post(
        "/jobs",
        json={"workflow": ["frame_extraction"], "payload": {"a": 2}},
        headers={"Idempotency-Key": key},
    )

    assert first.status_code == 201
    cleanup_job_ids.append(uuid.UUID(first.json()["id"]))
    assert second.status_code == 422


def test_get_job_returns_created_job(client: TestClient, cleanup_job_ids: list) -> None:
    created = client.post(
        "/jobs",
        json={"workflow": ["frame_extraction"], "payload": {}},
        headers={"Idempotency-Key": _key()},
    ).json()
    cleanup_job_ids.append(uuid.UUID(created["id"]))

    response = client.get(f"/jobs/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_job_is_404(client: TestClient) -> None:
    response = client.get(f"/jobs/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_created_job_event_gets_published_end_to_end(
    client: TestClient, cleanup_job_ids: list, database_url: str
) -> None:
    """The full chain: POST /jobs -> outbox row -> publisher -> Kafka.

    Polls the database rather than sleeping a fixed amount, bounded by the
    app's own outbox_poll_interval_seconds plus headroom. Uses its own
    engine for the same reason as cleanup_job_ids above.
    """
    created = client.post(
        "/jobs",
        json={"workflow": ["frame_extraction"], "payload": {}},
        headers={"Idempotency-Key": _key()},
    ).json()
    job_id = uuid.UUID(created["id"])
    cleanup_job_ids.append(job_id)

    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        status = None
        for _ in range(20):
            status = (
                await conn.execute(
                    sa.select(OutboxEvent.status).where(OutboxEvent.aggregate_id == job_id)
                )
            ).scalar_one()
            if status == "published":
                break
            await asyncio.sleep(0.5)
        assert status == "published"
    await engine.dispose()
