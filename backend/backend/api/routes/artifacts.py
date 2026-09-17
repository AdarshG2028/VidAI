"""Artifact download — the first endpoint in this API that serves bytes.

Until Phase 5 nothing needed one: `dummy` produced no files, and video
analysis only ever read them. Storage URIs are opaque `local://...`
strings that no client can dereference, so without this route a finished
job's output is unreachable except by looking in ./data/storage by hand.

Flat rather than nested under /jobs/{id}: an artifact is addressed by its
storage URI, which is already globally unique, and the same object can be
referenced by several stages once assets are forwarded down a chain
(media.forward_assets) -- so there is no single owning job to nest under.

**Range requests are supported**, which for a video product is not a nicety:
without them a <video> element can play a file but cannot seek, so
scrubbing through a render to check the crop framing or caption timing --
the entire point of reviewing output -- silently does nothing. The actual
streaming/redirect mechanics live in backend/services/media_streaming.py,
shared with the raw-video-preview route (backend/api/routes/projects.py),
which needs byte-for-byte the same behavior once a caller is authorized.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from backend.api.deps import ArtifactAccessDep, SessionDep
from backend.services.media_streaming import stream_storage_object

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts", response_model=None)
async def download_artifact(
    request: Request, uri: ArtifactAccessDep, session: SessionDep
) -> StreamingResponse | RedirectResponse:
    """Stream a stored artifact, honouring Range requests.

    Both the `uri` and the `job_id` it is authorized against are declared
    by require_artifact_access (backend/api/deps.py), which returns the
    URI once the caller is confirmed to be a member of the room that job
    belongs to. Phase 8: before this, anyone who could name a URI could
    fetch its bytes.

    The URI arrives whole, exactly as the listing handed it out — this
    route never parses or reconstructs one, per backend/storage/base.py's
    opaque-URI contract.

    `session` is declared only to be *released*: it is the same
    request-scoped session require_artifact_access already used for its
    membership check (FastAPI caches a dependency per request), and
    get_session is a yield-dependency, so FastAPI would otherwise hold its
    pooled connection until the response is fully sent -- which for a
    video means the whole download. A browser <video> opens several
    parallel range requests and holds them open while buffering and
    seeking, so a couple of viewers were enough to exhaust the pool
    (QueuePool limit of size 10 overflow 5) and make *every* endpoint in
    the API start timing out. Nothing below needs the database, so the
    connection goes back to the pool before the first byte is streamed.
    close() is idempotent -- get_session's own context manager closing it
    again on exit is harmless.
    """
    await session.close()
    return await stream_storage_object(request, uri)
