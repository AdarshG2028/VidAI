"""VideoAssetRepository (Phase 10 foundation)."""

import uuid

import pytest

from backend.models import Project, Video
from backend.repositories.video_asset_repository import VideoAssetRepository


@pytest.fixture
async def video_id(session) -> uuid.UUID:
    project = Project(id=uuid.uuid4(), owner_id=uuid.uuid4(), name="p")
    video = Video(
        id=uuid.uuid4(),
        project_id=project.id,
        storage_uri="local://abc.mp4",
        original_filename="abc.mp4",
    )
    session.add(project)
    session.add(video)
    await session.commit()
    return video.id


@pytest.mark.asyncio
async def test_get_returns_none_for_an_unseen_kind(session, video_id) -> None:
    repo = VideoAssetRepository(session)

    assert await repo.get(video_id, "transcript") is None


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips(session, video_id) -> None:
    repo = VideoAssetRepository(session)

    await repo.upsert(video_id, "transcript", "local://t.json")
    await session.commit()

    found = await repo.get(video_id, "transcript")
    assert found is not None
    assert found.uri == "local://t.json"


@pytest.mark.asyncio
async def test_upsert_replaces_rather_than_duplicates(session, video_id) -> None:
    """A re-run must overwrite the prior entry, not add a second row for
    the same (video_id, kind) -- this is a cache, not a history."""
    repo = VideoAssetRepository(session)

    await repo.upsert(video_id, "transcript", "local://first.json")
    await session.commit()
    await repo.upsert(video_id, "transcript", "local://second.json")
    await session.commit()

    found = await repo.get(video_id, "transcript")
    assert found.uri == "local://second.json"


@pytest.mark.asyncio
async def test_different_kinds_for_the_same_video_coexist(session, video_id) -> None:
    repo = VideoAssetRepository(session)

    await repo.upsert(video_id, "transcript", "local://t.json")
    await repo.upsert(video_id, "srt", "local://c.srt")
    await session.commit()

    assert (await repo.get(video_id, "transcript")).uri == "local://t.json"
    assert (await repo.get(video_id, "srt")).uri == "local://c.srt"


@pytest.mark.asyncio
async def test_upsert_can_carry_structured_data(session, video_id) -> None:
    repo = VideoAssetRepository(session)

    await repo.upsert(video_id, "embedding", "local://e.bin", data={"dims": 384})
    await session.commit()

    found = await repo.get(video_id, "embedding")
    assert found.data == {"dims": 384}
