import asyncio
import os
import re
import shutil

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.api.main import create_app
from backend.models import Project
from backend.core.config import get_settings
from backend.services.planner_factory import get_default_planner


@pytest.fixture
async def cleanup_project_ids(database_url: str):
    """Projects to delete afterwards. Shared by every room-level test file.

    Deleting the project cascades to its members, conversation, videos and
    project_jobs rows, which is why appending one id is enough.
    """
    created = []
    yield created
    if not created:
        return
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        for project_id in created:
            await conn.execute(sa.delete(Project).where(Project.id == project_id))
        await conn.commit()
    await engine.dispose()


def as_user(user_id) -> dict[str, str]:
    """Headers identifying the caller (Phase 8).

    Identity moved out of the request body into the X-User-Id header,
    because FastAPI resolves Pydantic bodies after dependencies and a
    membership guard therefore cannot read a body field. Tests that talk
    to a room endpoint need this or they get 422.
    """
    return {"X-User-Id": str(user_id)}


def _database_reachable(url: str) -> bool:
    async def check() -> bool:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(check())
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    url = str(get_settings().database_url)
    if not _database_reachable(url):
        pytest.skip(
            "Postgres not reachable — start it with "
            "`docker compose -f docker/docker-compose.yml up -d`",
            allow_module_level=True,
        )
    return url


@pytest.fixture
async def session(database_url: str):
    """A session whose commits are undone after the test.

    Bound to a connection-level transaction with join_transaction_mode=
    "create_savepoint": session.commit() only releases a SAVEPOINT and opens
    a new one, so code under test (like JobSubmissionService, which commits
    for real) still gets real commit semantics, but the outer rollback below
    erases everything once the test ends.
    """
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        await conn.begin()
        maker = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as s:
            yield s
        await conn.rollback()
    await engine.dispose()


def _kafka_reachable(bootstrap_servers: str) -> bool:
    from aiokafka import AIOKafkaProducer

    async def check() -> bool:
        producer = AIOKafkaProducer(bootstrap_servers=bootstrap_servers)
        try:
            await asyncio.wait_for(producer.start(), timeout=5)
            return True
        except Exception:
            return False
        finally:
            try:
                await producer.stop()
            except Exception:
                pass

    try:
        return asyncio.run(check())
    except Exception:
        return False


@pytest.fixture(scope="session")
def kafka_bootstrap_servers() -> str:
    servers = get_settings().kafka_bootstrap_servers
    if not _kafka_reachable(servers):
        pytest.skip(
            "Kafka not reachable — start it with "
            "`docker compose -f docker/docker-compose.yml up -d`",
            allow_module_level=True,
        )
    return servers


# Every Kafka-touching test names its topic with a fresh uuid4 so runs can't
# collide. Nothing deleted them, so they accumulated in the dev Redpanda
# across every run ever made against it -- and Redpanda's compose config
# (--memory=1G --smp=1) caps it at 256 partitions. Past that limit topic
# creation starts failing and Kafka tests fail with InvalidPartitionsError:
# spurious failures that look exactly like a real regression, in tests
# unrelated to whatever was actually being changed. This has already cost
# two debugging detours.
_EPHEMERAL_TOPIC = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


async def _delete_ephemeral_topics(servers: str) -> int:
    from aiokafka.admin import AIOKafkaAdminClient

    admin = AIOKafkaAdminClient(bootstrap_servers=servers)
    try:
        await admin.start()
    except Exception:
        return 0  # Kafka isn't up; there's nothing to have cleaned.
    try:
        # Matches anywhere in the name, so a "<uuid-topic>.dlq" sibling --
        # created implicitly by the harness, never named by a test -- is
        # collected too. Real topics (crop, video_analysis, dummy, ...)
        # carry no uuid and are never touched.
        doomed = [topic for topic in await admin.list_topics() if _EPHEMERAL_TOPIC.search(topic)]
        if doomed:
            await admin.delete_topics(doomed)
        return len(doomed)
    except Exception:
        return 0
    finally:
        await admin.close()


@pytest.fixture(scope="session", autouse=True)
def _purge_ephemeral_kafka_topics():
    """Delete this run's throwaway Kafka topics once the session ends.

    Session-scoped and pattern-based rather than per-test cleanup: the
    `.dlq` topics are created by the worker harness, not by the test that
    named the parent topic, so a test tidying up only what it explicitly
    created would leave half the mess behind. Silently does nothing when
    Kafka is unreachable, so the no-Docker workflow is unaffected.
    """
    yield
    try:
        cleaned = asyncio.run(_delete_ephemeral_topics(get_settings().kafka_bootstrap_servers))
    except Exception:
        return
    if cleaned:
        print(f"\n[conftest] deleted {cleaned} ephemeral Kafka topic(s)")


@pytest.fixture(scope="session", autouse=True)
def _no_ambient_groq_key():
    """The test suite must stay deterministic (StaticPlanner) regardless of
    a developer's local .env -- GROQ_API_KEY there is for manually running
    the dev server (see planner_factory.get_default_planner), not for
    pytest, which would otherwise make real Groq calls and break every
    assertion written against StaticPlanner's fixed output (discovered live,
    2026-07-28: adding a real key to .env for manual testing silently
    flipped the whole suite over to the real LLM planner). get_settings and
    get_default_planner are both @lru_cache, so this only works if it runs
    before either is ever called -- session-scoped autouse, and `client`
    below explicitly depends on it to guarantee the ordering.
    """
    # Set (not pop!) to an empty string: pydantic-settings' precedence is
    # environment variable > .env file, so an *unset* os.environ var still
    # falls through to .env's real key -- only an explicit override in
    # os.environ actually beats it. Confirmed this the hard way: popping
    # alone did not stop the suite from using the real key.
    had_key = "GROQ_API_KEY" in os.environ
    original = os.environ.get("GROQ_API_KEY")
    os.environ["GROQ_API_KEY"] = ""
    get_settings.cache_clear()
    get_default_planner.cache_clear()
    yield
    if had_key:
        os.environ["GROQ_API_KEY"] = original
    else:
        del os.environ["GROQ_API_KEY"]
    get_settings.cache_clear()
    get_default_planner.cache_clear()


@pytest.fixture(scope="session")
def client(_no_ambient_groq_key):
    """One TestClient (one portal event loop) for the *entire test session*,
    shared by every module that hits the real app (test_jobs_api.py,
    test_videos_api.py, ...).

    backend.database.session.get_engine() is a process-wide singleton
    (@lru_cache), matching production where the app runs once. Two
    module-scoped clients — one per test file — would each spin up their
    own portal thread/event loop while both still draw connections from
    that one shared engine's pool: a connection opened under the first
    module's loop can get reused and torn down under the second module's
    different loop, which asyncpg/SQLAlchemy can't do ("RuntimeError: Event
    loop is closed", surfacing only once a second such module exists).
    Session-scoping this fixture in conftest.py means there is only ever
    one portal for the whole run, regardless of how many test files use it.
    """
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="session")
def ffprobe_available() -> None:
    """VideoAnalysisWorker shells out to a real ffprobe binary — tests that
    exercise that need it installed and on PATH, same skip-cleanly pattern
    as the Postgres/Kafka fixtures above."""
    if shutil.which("ffprobe") is None:
        pytest.skip(
            "ffprobe not on PATH — install ffmpeg to run this test",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def ffmpeg_available() -> None:
    """Phase 5's media capabilities shell out to a real ffmpeg binary.
    Separate from ffprobe_available because they're separate binaries: a
    given box can have one without the other, and a test that only probes
    shouldn't skip because ffmpeg is missing (or vice versa)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip(
            "ffmpeg not on PATH — install ffmpeg to run this test",
            allow_module_level=True,
        )
