"""Artifact garbage collection.

Every stage stores a full copy of its output and nothing ever deleted
any of it, so a six-stage job left roughly six videos behind forever.

Most of these tests are about what must *survive*: a sweep that frees
space is easy, and a sweep that deletes a user's source video is a data
loss bug that no amount of reclaimed disk excuses.
"""

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Job, JobStatus, Project, Result, Video
from backend.services.artifact_cleanup_service import ArtifactCleanupService, _asset_uris
from backend.storage.local import LocalDiskStorage

pytestmark = pytest.mark.usefixtures("database_url")


@pytest.fixture
async def engine(database_url: str):
    eng = create_async_engine(database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.services.artifact_cleanup_service.get_storage", lambda: disk)
    return disk


def _assets(*uris: str) -> dict:
    return {"assets": [{"kind": "video", "uri": uri} for uri in uris]}


async def _finished_job(
    sessionmaker,
    stages: list[dict],
    *,
    age_hours: float = 48,
    status: JobStatus = JobStatus.COMPLETED,
) -> uuid.UUID:
    """A terminal job whose updated_at is far enough back to be sweepable."""
    job_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            Job(
                id=job_id,
                status=status,
                workflow={"workflow": ["a"] * len(stages)},
                current_stage=len(stages),
                payload={},
                max_attempts=3,
            )
        )
        await session.flush()
        for stage, payload in enumerate(stages):
            session.add(
                Result(job_id=job_id, worker_name=f"w{stage}", stage=stage, payload=payload)
            )
        await session.commit()

    async with sessionmaker() as session:
        await session.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours))
        )
        await session.commit()
    return job_id


async def _cleanup(engine, job_ids: list[uuid.UUID], owner: uuid.UUID | None = None) -> None:
    async with engine.begin() as conn:
        for job_id in job_ids:
            await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        if owner:
            await conn.execute(
                sa.text("DELETE FROM projects WHERE owner_id = :o"), {"o": owner}
            )


# --- payload parsing -------------------------------------------------------


def test_asset_uris_tolerates_anything() -> None:
    """Reclamation infrastructure must not throw on a surprising payload —
    one odd row would otherwise stall every later sweep."""
    assert _asset_uris(None) == set()
    assert _asset_uris({"processed_by": "dummy"}) == set()
    assert _asset_uris({"assets": "nope"}) == set()
    assert _asset_uris({"assets": [{"kind": "video"}]}) == set()
    assert _asset_uris(_assets("local://a.mp4")) == {"local://a.mp4"}


# --- what gets freed -------------------------------------------------------


@pytest.mark.asyncio
async def test_intermediates_go_and_the_final_output_stays(engine, sessionmaker, storage) -> None:
    mid1 = storage.put(b"stage0", suggested_name="a.mp4")
    mid2 = storage.put(b"stage1", suggested_name="b.mp4")
    final = storage.put(b"final", suggested_name="c.mp4")
    job_id = await _finished_job(
        sessionmaker, [_assets(mid1), _assets(mid2), _assets(final)]
    )

    try:
        async with sessionmaker() as session:
            outcome = await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert outcome.artifacts_deleted == 2
        assert not storage.exists(mid1)
        assert not storage.exists(mid2)
        assert storage.exists(final), "the deliverable must survive"
    finally:
        await _cleanup(engine, [job_id])


@pytest.mark.asyncio
async def test_a_job_is_only_swept_once(engine, sessionmaker, storage) -> None:
    """Without the marker the sweeper rescans every finished job on every
    pass, forever."""
    job_id = await _finished_job(
        sessionmaker,
        [_assets(storage.put(b"x", suggested_name="a.mp4")), _assets("local://final.mp4")],
    )

    try:
        async with sessionmaker() as session:
            first = await ArtifactCleanupService(session).sweep(retention_hours=24)
        async with sessionmaker() as session:
            second = await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert first.jobs_swept == 1
        assert second.jobs_swept == 0
    finally:
        await _cleanup(engine, [job_id])


@pytest.mark.asyncio
async def test_dead_lettered_jobs_are_swept_too(engine, sessionmaker, storage) -> None:
    """A failed job's leftovers are exactly the ones nobody wants kept —
    and completed_at is never set on one, which is why the sweep keys off
    updated_at instead."""
    orphan = storage.put(b"partial", suggested_name="a.mp4")
    job_id = await _finished_job(
        sessionmaker,
        [_assets(orphan), _assets("local://b.mp4")],
        status=JobStatus.DEAD_LETTERED,
    )

    try:
        async with sessionmaker() as session:
            outcome = await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert outcome.jobs_swept == 1
        assert not storage.exists(orphan)
    finally:
        await _cleanup(engine, [job_id])


# --- what must survive -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_users_source_video_is_never_deleted(engine, sessionmaker, storage) -> None:
    """The one that would be data loss rather than housekeeping.

    transcribe re-encodes nothing and reports its *input* URI as its
    output, so a user's own upload genuinely appears in an intermediate
    Result — a sweep that only looked at stage index would delete it.
    """
    source = storage.put(b"the user's upload", suggested_name="mine.mp4")
    owner = uuid.uuid4()
    project_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Project(id=project_id, owner_id=owner))
        await session.flush()
        session.add(
            Video(
                project_id=project_id,
                storage_uri=source,
                original_filename="mine.mp4",
            )
        )
        await session.commit()

    job_id = await _finished_job(
        sessionmaker, [_assets(source), _assets(storage.put(b"edited", suggested_name="e.mp4"))]
    )

    try:
        async with sessionmaker() as session:
            await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert storage.exists(source), "deleted a user's own upload"
    finally:
        await _cleanup(engine, [job_id], owner)


@pytest.mark.asyncio
async def test_an_artifact_another_job_still_uses_is_kept(
    engine, sessionmaker, storage
) -> None:
    """Preview and confirm are compiled from the same proposal against the
    same source, so they share URIs by construction. Sweeping one must not
    break the other."""
    shared = storage.put(b"shared", suggested_name="s.mp4")
    old_job = await _finished_job(
        sessionmaker, [_assets(shared), _assets("local://old-final.mp4")]
    )
    recent_job = await _finished_job(
        sessionmaker, [_assets(shared)], age_hours=0
    )

    try:
        async with sessionmaker() as session:
            await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert storage.exists(shared), "deleted an artifact another job points at"
    finally:
        await _cleanup(engine, [old_job, recent_job])


@pytest.mark.asyncio
async def test_recent_jobs_are_left_alone(engine, sessionmaker, storage) -> None:
    """Intermediates are what make "which stage got it wrong" answerable,
    so they survive a retention window before being reclaimed."""
    mid = storage.put(b"fresh", suggested_name="a.mp4")
    job_id = await _finished_job(
        sessionmaker, [_assets(mid), _assets("local://f.mp4")], age_hours=1
    )

    try:
        async with sessionmaker() as session:
            outcome = await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert outcome.jobs_swept == 0
        assert storage.exists(mid)
    finally:
        await _cleanup(engine, [job_id])


@pytest.mark.asyncio
async def test_a_running_job_is_never_touched(engine, sessionmaker, storage) -> None:
    """Its next stage is about to read exactly these files."""
    mid = storage.put(b"in flight", suggested_name="a.mp4")
    job_id = await _finished_job(
        sessionmaker, [_assets(mid)], status=JobStatus.RUNNING
    )

    try:
        async with sessionmaker() as session:
            outcome = await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert outcome.jobs_swept == 0
        assert storage.exists(mid)
    finally:
        await _cleanup(engine, [job_id])


@pytest.mark.asyncio
async def test_a_single_stage_job_loses_nothing(engine, sessionmaker, storage) -> None:
    """Its only stage is also its final one."""
    only = storage.put(b"single", suggested_name="a.mp4")
    job_id = await _finished_job(sessionmaker, [_assets(only)])

    try:
        async with sessionmaker() as session:
            await ArtifactCleanupService(session).sweep(retention_hours=24)

        assert storage.exists(only)
    finally:
        await _cleanup(engine, [job_id])
