"""The room WebSocket (Phase 8): `WS /projects/{id}/ws`.

One socket per client per room, membership-checked at connect. It carries
notifications only -- `{seq, type, data}` envelopes describing writes that
have already been committed through REST. Nothing a client sends is read.

**Why a separate module** from `routes/projects.py`: everything there is
request/response, and this is a long-lived connection with its own
lifecycle, close codes and failure modes. Mixing them would bury the one
endpoint whose error handling looks nothing like the others'.

**Reconnect** is `GET /projects/{id}` for a fresh snapshot -- which
reports the same `seq` counter this stream advances -- and then resuming
here. Connect before snapshotting and discard events at or below the
snapshot's `seq`, and the handoff has no window in which an event can be
missed.
"""

import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.api.deps import SessionDep, as_user_id
from backend.repositories.project_member_repository import ProjectMemberRepository
from backend.services.room_events import CLOSE, get_room_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])

# Policy violation. Used for every refusal, including the two that must
# stay indistinguishable: a non-member and a room that does not exist get
# the identical code and reason, for the same reason
# require_project_member returns 404 rather than 403 -- otherwise the
# handshake reveals whether a room id is real.
_POLICY_VIOLATION = 1008
# Sent when a client fell behind far enough to lose events. Distinct
# because the client's correct response is distinct: refetch the snapshot
# before resuming, rather than simply reconnecting.
_TRY_AGAIN_LATER = 1013
# The server is shutting down. An ordinary reconnect is the right answer,
# and no snapshot refetch is implied -- nothing was dropped.
_GOING_AWAY = 1001


@router.websocket("/{project_id}/ws")
async def room_socket(websocket: WebSocket, project_id: uuid.UUID, session: SessionDep) -> None:
    """Stream a room's events to one member.

    Identity comes from the `user_id` query parameter: a browser cannot
    put headers on `new WebSocket(url)`, which is what the header/query
    equivalence in backend/api/deps.py exists for.

    Both checks happen *before* accept, so a rejected client sees a failed
    handshake rather than a connection that opens and immediately dies.
    """
    user_id = as_user_id(websocket.query_params.get("user_id"))
    if user_id is None:
        await websocket.close(code=_POLICY_VIOLATION, reason="identity required")
        return

    if not await ProjectMemberRepository(session).is_member(project_id, user_id):
        await websocket.close(code=_POLICY_VIOLATION, reason="project not found")
        return

    # Release the pooled connection before the socket opens. get_session is
    # a yield-dependency, so it is held until this handler *returns* -- and
    # this one returns when the client disconnects, which may be hours. The
    # membership check above is the only query here, so a room left open in
    # a browser tab was pinning a connection for nothing; a handful of tabs
    # exhausted the pool (size 10 + overflow 5) and every database-backed
    # endpoint in the API began timing out while /health stayed green.
    # Same failure as the streaming routes -- see media_streaming.py.
    await session.close()

    await websocket.accept()

    events = get_room_events()
    subscription = events.subscribe(project_id)
    try:
        while True:
            item = await subscription.queue.get()
            if item is CLOSE:
                code = _TRY_AGAIN_LATER if subscription.overflowed else _GOING_AWAY
                await websocket.close(code=code)
                return
            await websocket.send_json(item)
    except WebSocketDisconnect:
        # The ordinary ending: the client navigated away or refreshed.
        pass
    finally:
        events.unsubscribe(subscription)
