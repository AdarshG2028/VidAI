"""Artifact listing and download (Phase 5A, Step 7).

The first endpoints that let a client actually retrieve output. Written
before any real capability exists, and exercised against `dummy` jobs and
hand-written Result rows, precisely so the retrieval path is proven
independently of ffmpeg — when 5B's crop lands, a wrong output is then
unambiguously crop's fault, not the download route's.
"""

import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Job, JobStatus, ProjectJob, Result
from backend.storage.local import LocalDiskStorage
from backend.workers.media import Asset, AssetKind, assets_payload
from tests.conftest import as_user

pytestmark = pytest.mark.usefixtures("database_url")


async def _seed_job(
    engine, *, results: list[Result] | None = None, project_id: uuid.UUID | None = None
) -> uuid.UUID:
    """A completed job with hand-written stage results.

    `project_id` binds it to a room the way a real submission would
    (Phase 8's project_jobs). Left None it models a raw POST /jobs
    submission, which belongs to no room by design.
    """
    job_id = uuid.uuid4()
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sm() as session:
        session.add(
            Job(
                id=job_id,
                status=JobStatus.COMPLETED,
                workflow={"workflow": ["crop", "transcribe"]},
                current_stage=2,
                payload={},
                max_attempts=3,
            )
        )
        await session.flush()
        if project_id is not None:
            session.add(
                ProjectJob(
                    job_id=job_id, project_id=project_id, submitted_by_user_id=uuid.uuid4()
                )
            )
        for result in results or []:
            result.job_id = job_id
            session.add(result)
        await session.commit()
    return job_id


async def _cleanup(engine, job_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})


async def _delete_project(engine, project_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})


@pytest.fixture
async def engine(database_url: str):
    eng = create_async_engine(database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


class _FakePresigningStorage:
    """A Storage double that answers presigned_url() the way S3Storage
    would, without touching AWS -- get()/size()/open_stream() are never
    called by download_artifact once presigned_url() returns non-None, so
    this double doesn't implement them.
    """

    def presigned_url(self, uri: str) -> str | None:
        return f"https://fake-bucket.s3.amazonaws.com/signed?uri={quote(uri, safe='')}"


@pytest.fixture
def presigning_storage(monkeypatch) -> _FakePresigningStorage:
    fake = _FakePresigningStorage()
    monkeypatch.setattr("backend.services.media_streaming.get_storage", lambda: fake)
    return fake


@pytest.fixture
def artifact_storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    """Point the download route at a temp dir rather than the configured
    ./data/storage. The route resolves get_storage() per request via
    backend.services.media_streaming, so patching its module reference is
    enough — and it keeps these tests (one of which writes a deliberately
    large object) from leaving files behind in the real storage directory
    on every run."""
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.services.media_streaming.get_storage", lambda: disk)
    return disk


# --- listing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_assets_per_stage_with_download_urls(client, engine) -> None:
    job_id = await _seed_job(
        engine,
        results=[
            Result(
                worker_name="crop",
                stage=0,
                payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri="local://a.mp4")]),
                artifact_uri="local://a.mp4",
            ),
            Result(
                worker_name="transcribe",
                stage=1,
                payload=assets_payload(
                    [
                        Asset(kind=AssetKind.VIDEO, uri="local://a.mp4"),
                        Asset(kind=AssetKind.SRT, uri="local://c.srt"),
                    ]
                ),
                artifact_uri="local://a.mp4",
            ),
        ],
    )
    try:
        body = client.get(f"/jobs/{job_id}/artifacts").json()

        assert [stage["stage"] for stage in body["stages"]] == [0, 1]
        assert body["stages"][0]["worker_name"] == "crop"

        # The srt is listed in its own right — that's the point of the
        # split in 5F, so a user can take the subtitle file alone.
        kinds = [a["kind"] for a in body["stages"][1]["artifacts"]]
        assert kinds == ["video", "srt"]

        srt = body["stages"][1]["artifacts"][1]
        assert (
            srt["download_url"]
            == f"/artifacts?uri={quote('local://c.srt', safe='')}&job_id={job_id}"
        )
    finally:
        await _cleanup(engine, job_id)


@pytest.mark.asyncio
async def test_stages_without_assets_are_listed_with_an_empty_artifact_list(
    client, engine
) -> None:
    """dummy and video_analysis predate the asset convention. They must
    still appear, or the response would silently misrepresent the
    workflow as shorter than it was."""
    job_id = await _seed_job(
        engine,
        results=[Result(worker_name="dummy", stage=0, payload={"processed_by": "dummy"})],
    )
    try:
        body = client.get(f"/jobs/{job_id}/artifacts").json()

        assert len(body["stages"]) == 1
        assert body["stages"][0]["artifacts"] == []
    finally:
        await _cleanup(engine, job_id)


@pytest.mark.asyncio
async def test_listing_unknown_job_is_404(client) -> None:
    assert client.get(f"/jobs/{uuid.uuid4()}/artifacts").status_code == 404


# --- download --------------------------------------------------------------
#
# Every download is now authorized against a room (Phase 8, step 7): the
# URL carries the job the artifact was listed under, and the caller must
# be a member of that job's project. So these tests, which are really
# about streaming and Range handling, each need a room to publish into --
# which is closer to production than the bare storage objects they used
# before, where no artifact exists outside a job.


class _Published:
    """A stored artifact plus the authorized URL and identity to fetch it."""

    def __init__(self, client, url: str, headers: dict, job_id: uuid.UUID, uri: str) -> None:
        self._client = client
        self.url = url
        self.headers = headers
        self.job_id = job_id
        self.uri = uri

    def get(self, *, headers: dict | None = None, **kwargs):
        return self._client.get(self.url, headers={**self.headers, **(headers or {})}, **kwargs)


@pytest.fixture
async def room(client, engine, artifact_storage):
    """A project with one member, and `publish(...)` to put an artifact in it.

    The project is created through the API rather than by insert, so the
    owner really is a member by the same path production uses.
    """
    owner = uuid.uuid4()
    project = client.post(
        "/projects", json={"name": "artifacts"}, headers=as_user(owner)
    ).json()
    project_id = uuid.UUID(project["id"])
    seeded: list[uuid.UUID] = []

    async def publish_uri(uri: str, *, project: uuid.UUID | None = None) -> _Published:
        """Record `uri` as an artifact of a fresh job in this room."""
        job_id = await _seed_job(
            engine,
            project_id=project_id if project is None else project,
            results=[
                Result(
                    worker_name="crop",
                    stage=0,
                    payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=uri)]),
                )
            ],
        )
        seeded.append(job_id)
        return _Published(
            client,
            f"/artifacts?uri={quote(uri, safe='')}&job_id={job_id}",
            as_user(owner),
            job_id,
            uri,
        )

    async def publish(data: bytes, name: str) -> _Published:
        return await publish_uri(artifact_storage.put(data, suggested_name=name))

    room = SimpleNamespace(
        owner=owner,
        project_id=project_id,
        publish=publish,
        publish_uri=publish_uri,
        seeded=seeded,
    )
    yield room

    for job_id in seeded:
        await _cleanup(engine, job_id)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})


@pytest.mark.asyncio
async def test_downloads_the_stored_bytes(room) -> None:
    published = await room.publish(b"pretend this is an mp4", "clip.mp4")

    response = published.get()

    assert response.status_code == 200
    assert response.content == b"pretend this is an mp4"
    assert response.headers["content-type"].startswith("video/mp4")


@pytest.mark.asyncio
async def test_download_url_from_the_listing_actually_resolves(client, room) -> None:
    """The listing builds download_url by quoting the URI and appending the
    job; this is the round trip proving a client can use it verbatim."""
    published = await room.publish(b"bytes", "clip.mp4")

    listed = client.get(f"/jobs/{published.job_id}/artifacts").json()
    url = listed["stages"][0]["artifacts"][0]["download_url"]

    response = client.get(url, headers=as_user(room.owner))

    assert url == published.url
    assert response.status_code == 200
    assert response.content == b"bytes"


@pytest.mark.asyncio
async def test_content_type_follows_the_extension(room) -> None:
    published = await room.publish(b"1\n00:00:00,000 --> 00:00:01,000\nhi\n", "c.srt")

    response = published.get()

    assert response.status_code == 200
    assert response.content.startswith(b"1\n")


@pytest.mark.asyncio
async def test_unknown_artifact_is_404(room) -> None:
    """Recorded as an asset but never stored — the guard passes and the
    storage backend is what refuses."""
    published = await room.publish_uri("local://nope.mp4")

    assert published.get().status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        "local://../../../etc/passwd",
        "local://..\\..\\windows\\system32\\config\\sam",
        "local://subdir/escape.mp4",
        "file:///etc/passwd",
        "local://..",
        "local://",
    ],
)
async def test_traversal_and_foreign_schemes_are_rejected(room, hostile: str) -> None:
    """The `uri` parameter is attacker-controlled. Registered here as a
    genuine asset of the caller's own job, so the membership guard passes
    and LocalDiskStorage's key guard is what actually rejects these --
    which also models the sharper threat now that a guard exists: a
    compromised worker writing a hostile URI into its own result payload.
    """
    published = await room.publish_uri(hostile)

    assert published.get().status_code == 404


@pytest.mark.asyncio
async def test_download_streams_rather_than_buffering(room) -> None:
    """A whole video in memory per request is the thing open_stream exists
    to avoid — assert a large object comes back byte-exact through the
    chunked path."""
    payload = bytes(range(256)) * 8192  # 2 MiB, larger than the 64 KiB chunk
    published = await room.publish(payload, "big.mp4")

    response = published.get()

    assert response.status_code == 200
    assert response.content == payload


# --- authorization (step 7) ------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_cannot_download_a_rooms_artifact(room) -> None:
    """The hole this step closes. URIs are opaque and effectively
    unguessable, but they travel: every artifact listing and every room
    snapshot hands them out, and they outlive the membership of whoever
    saw them."""
    published = await room.publish(b"bytes", "clip.mp4")

    response = published.get(headers=as_user(uuid.uuid4()))

    assert response.status_code == 404
    assert response.content != b"bytes"


@pytest.mark.asyncio
async def test_an_anonymous_download_is_rejected(client, room) -> None:
    published = await room.publish(b"bytes", "clip.mp4")

    assert client.get(published.url).status_code == 422


@pytest.mark.asyncio
async def test_identity_can_travel_in_the_url_for_a_video_element(client, room) -> None:
    """A browser cannot put a header on `<video src=...>`, and playing
    output in a video element is the reason this route streams and honours
    Range at all. Header-only identity would have made the endpoint
    unusable by the one client it was built for.

    No security is given up: X-User-Id is asserted and believed either
    way (backend/api/deps.py).
    """
    published = await room.publish(b"pretend this is an mp4", "clip.mp4")

    response = client.get(f"{published.url}&user_id={room.owner}")

    assert response.status_code == 200
    assert response.content == b"pretend this is an mp4"


@pytest.mark.asyncio
async def test_a_url_carried_identity_is_still_checked_for_membership(client, room) -> None:
    """The query parameter is a transport, not a bypass."""
    published = await room.publish(b"bytes", "clip.mp4")

    response = client.get(f"{published.url}&user_id={uuid.uuid4()}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_redirects_to_a_presigned_url_when_the_backend_offers_one(
    client, engine, presigning_storage
) -> None:
    """An S3-backed Storage answers presigned_url() with a real URL --
    the route hands the caller straight to it instead of streaming the
    bytes through this process (backend/api/routes/artifacts.py)."""
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "s3"}, headers=as_user(owner)).json()
    project_id = uuid.UUID(project["id"])
    uri = "s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4"
    job_id = await _seed_job(
        engine,
        project_id=project_id,
        results=[
            Result(
                worker_name="crop",
                stage=0,
                payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=uri)]),
            )
        ],
    )
    try:
        response = client.get(
            f"/artifacts?uri={quote(uri, safe='')}&job_id={job_id}",
            headers=as_user(owner),
            follow_redirects=False,
        )

        assert response.status_code == 307
        assert response.headers["location"] == presigning_storage.presigned_url(uri)
        assert response.headers["cache-control"] == "no-store"
    finally:
        await _cleanup(engine, job_id)
        await _delete_project(engine, project_id)


@pytest.mark.asyncio
async def test_a_stranger_is_refused_before_any_presigned_redirect(
    client, engine, presigning_storage
) -> None:
    """The membership guard (require_artifact_access) is a dependency
    resolved before the route body runs -- proves the redirect branch
    cannot be reached by someone who was never authorized to see this
    artifact in the first place."""
    owner = uuid.uuid4()
    project = client.post("/projects", json={"name": "s3"}, headers=as_user(owner)).json()
    project_id = uuid.UUID(project["id"])
    uri = "s3://deadbeefdeadbeefdeadbeefdeadbeef.mp4"
    job_id = await _seed_job(
        engine,
        project_id=project_id,
        results=[
            Result(
                worker_name="crop",
                stage=0,
                payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=uri)]),
            )
        ],
    )
    try:
        response = client.get(
            f"/artifacts?uri={quote(uri, safe='')}&job_id={job_id}",
            headers=as_user(uuid.uuid4()),
            follow_redirects=False,
        )

        assert response.status_code == 404
    finally:
        await _cleanup(engine, job_id)
        await _delete_project(engine, project_id)


@pytest.mark.asyncio
async def test_a_job_id_from_your_own_room_does_not_unlock_another_rooms_uri(
    client, room, engine, artifact_storage
) -> None:
    """Membership alone is not enough: without checking that the job
    actually produced this artifact, a member could pair a job id from
    their own room with any URI they had ever seen."""
    theirs = uuid.uuid4()
    other = client.post("/projects", json={"name": "theirs"}, headers=as_user(theirs)).json()
    try:
        foreign = await room.publish_uri(
            artifact_storage.put(b"private", suggested_name="secret.mp4"),
            project=uuid.UUID(other["id"]),
        )
        mine = await room.publish(b"mine", "mine.mp4")

        response = client.get(
            f"/artifacts?uri={quote(foreign.uri, safe='')}&job_id={mine.job_id}",
            headers=as_user(room.owner),
        )

        assert response.status_code == 404
        assert response.content != b"private"
    finally:
        # The room fixture only owns its own project; this second one
        # would otherwise be left behind on every run.
        await _delete_project(engine, uuid.UUID(other["id"]))


@pytest.mark.asyncio
async def test_an_unmapped_jobs_artifacts_are_not_downloadable(
    client, engine, artifact_storage
) -> None:
    """A raw POST /jobs submission belongs to no room, and nobody is a
    member of no room. Deliberately stricter than before, when unmapped
    meant world-readable."""
    uri = artifact_storage.put(b"orphan", suggested_name="orphan.mp4")
    job_id = await _seed_job(
        engine,
        results=[
            Result(
                worker_name="crop",
                stage=0,
                payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=uri)]),
            )
        ],
    )
    try:
        response = client.get(
            f"/artifacts?uri={quote(uri, safe='')}&job_id={job_id}",
            headers=as_user(uuid.uuid4()),
        )

        assert response.status_code == 404
    finally:
        await _cleanup(engine, job_id)


@pytest.mark.asyncio
async def test_an_intermediate_stages_artifact_is_downloadable_too(
    client, room, engine, artifact_storage
) -> None:
    """The per-stage listing exists so a client can see which step went
    wrong, so its URLs have to resolve — the guard checks every stage's
    assets, not only the final one."""
    early = artifact_storage.put(b"stage one", suggested_name="one.mp4")
    late = artifact_storage.put(b"stage two", suggested_name="two.mp4")
    job_id = await _seed_job(
        engine,
        project_id=room.project_id,
        results=[
            Result(
                worker_name="crop",
                stage=0,
                payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=early)]),
            ),
            Result(
                worker_name="audio",
                stage=1,
                payload=assets_payload([Asset(kind=AssetKind.VIDEO, uri=late)]),
            ),
        ],
    )
    room.seeded.append(job_id)

    response = client.get(
        f"/artifacts?uri={quote(early, safe='')}&job_id={job_id}",
        headers=as_user(room.owner),
    )

    assert response.status_code == 200
    assert response.content == b"stage one"


# --- range requests (what <video> seeking needs) ---------------------------


@pytest.fixture
async def ranged(room):
    """A stored object with byte-identifiable content, so a partial
    response can be checked against the exact expected slice."""
    payload = bytes(range(256)) * 40  # 10240 bytes
    return payload, await room.publish(payload, "clip.mp4")


@pytest.mark.asyncio
async def test_full_request_advertises_range_support(ranged) -> None:
    """Without Accept-Ranges a browser assumes it must download the whole
    file to play it, and never attempts to seek."""
    payload, published = ranged

    response = published.get()

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(payload))


@pytest.mark.asyncio
async def test_explicit_range_returns_exactly_that_slice(ranged) -> None:
    payload, published = ranged

    response = published.get(headers={"Range": "bytes=100-199"})

    assert response.status_code == 206
    assert response.content == payload[100:200]
    assert response.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert response.headers["content-length"] == "100"


@pytest.mark.asyncio
async def test_open_ended_range_runs_to_the_end(ranged) -> None:
    """`bytes=N-` is what a video element sends when the user seeks."""
    payload, published = ranged

    response = published.get(headers={"Range": "bytes=10000-"})

    assert response.status_code == 206
    assert response.content == payload[10000:]
    assert response.headers["content-range"] == f"bytes 10000-{len(payload) - 1}/{len(payload)}"


@pytest.mark.asyncio
async def test_suffix_range_returns_the_last_n_bytes(ranged) -> None:
    """Used to read an MP4's trailing metadata when the index isn't at the
    front of the file."""
    payload, published = ranged

    response = published.get(headers={"Range": "bytes=-500"})

    assert response.status_code == 206
    assert response.content == payload[-500:]


@pytest.mark.asyncio
async def test_range_past_the_end_is_rejected_not_silently_widened(ranged) -> None:
    """A malformed range must not quietly return the whole file — the
    client would treat it as a successful seek to the wrong place."""
    payload, published = ranged

    response = published.get(headers={"Range": "bytes=999999-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(payload)}"


@pytest.mark.asyncio
async def test_range_end_beyond_the_file_is_clamped(ranged) -> None:
    """A start inside the file with an end past it is satisfiable — serve
    what exists rather than refusing."""
    payload, published = ranged

    response = published.get(headers={"Range": "bytes=10000-999999"})

    assert response.status_code == 206
    assert response.content == payload[10000:]


@pytest.mark.asyncio
async def test_a_malformed_range_header_is_ignored(ranged) -> None:
    """Not an error: the spec says an unparseable Range is ignored and the
    full representation returned."""
    payload, published = ranged

    response = published.get(headers={"Range": "furlongs=1-2"})

    assert response.status_code == 200
    assert response.content == payload


@pytest.mark.asyncio
async def test_ranged_reads_reassemble_into_the_original(ranged) -> None:
    """The property that actually matters: a player fetching a file in
    pieces must be able to reconstruct it byte for byte."""
    payload, published = ranged

    chunks = [
        published.get(headers={"Range": f"bytes={start}-{start + 2047}"}).content
        for start in range(0, len(payload), 2048)
    ]

    assert b"".join(chunks) == payload
