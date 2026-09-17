"""In-process fan-out for room events (Phase 8).

**The socket is pure fan-out.** Nothing is ever written through it; REST
stays the only write path, and the database stays the only source of
truth for ordering. What travels here is notification that something has
already been committed, so a member watching a room does not have to
poll to find out.

**Envelope: `{seq, type, data}`.** `seq` is per-room and monotonic, and
it exists for one purpose -- letting a client notice it missed something.
The room snapshot (`GET /projects/{id}`) reports the same counter, so
"reconnect, refetch the snapshot, resume the stream" is a comparison
between two numbers from the same source rather than a leap of faith.
The counter lives in memory and restarts at zero with the process; that
is deliberate rather than overlooked, because a reconnecting client
always re-baselines against a fresh snapshot, so an epoch would be
ceremony protecting nothing.

**One process is the V1 reality.** The scale-out path is Redis pub/sub
between API processes, which replaces `_rooms` and leaves the envelope,
the event types and every caller untouched -- an extension of this
design, not a redesign.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["RoomEventBus", "RoomSubscription", "get_room_events"]

# Per-connection buffer. Sized for a burst -- a job progressing through a
# seven-stage workflow while somebody is talking -- not for a client that
# has stopped reading, which is what the overflow path is for.
_DEFAULT_QUEUE_SIZE = 64

# Pushed into a subscription's queue to end its send loop. A sentinel
# rather than cancelling the socket task: the loop owns the connection,
# so letting it finish normally keeps close handling in one place.
CLOSE = object()


# eq=False keeps identity hashing: subscriptions live in a set, and two
# clients that happen to be in the same room with equally-empty queues
# are still two clients.
@dataclass(eq=False)
class RoomSubscription:
    """One connected client's view of one room."""

    project_id: uuid.UUID
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(_DEFAULT_QUEUE_SIZE))
    # Set when this client fell far enough behind that events were lost.
    # The send loop reads it to pick a close code, so the client learns it
    # has a hole rather than carrying on quietly wrong.
    overflowed: bool = False


class RoomEventBus:
    def __init__(self, *, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._rooms: dict[uuid.UUID, set[RoomSubscription]] = {}
        # Never evicted, unlike _rooms: a room's sequence has to survive
        # its last subscriber leaving, or the next snapshot would report a
        # lower seq than events the client already saw and every reconnect
        # would look like a gap. One integer per room the process has
        # served is a cost worth paying for that.
        self._seq: dict[uuid.UUID, int] = {}

    def current_seq(self, project_id: uuid.UUID) -> int:
        """The last sequence number published to this room, 0 if none."""
        return self._seq.get(project_id, 0)

    def rooms(self) -> list[uuid.UUID]:
        """Rooms with at least one connected client.

        The progress poller works from this rather than from "rooms with
        running jobs": with nobody listening there is nothing to send, so
        polling the whole table would be work performed for no observer.
        """
        return [project_id for project_id, subs in self._rooms.items() if subs]

    def subscribe(self, project_id: uuid.UUID) -> RoomSubscription:
        subscription = RoomSubscription(
            project_id=project_id, queue=asyncio.Queue(self._queue_size)
        )
        self._rooms.setdefault(project_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: RoomSubscription) -> None:
        subscribers = self._rooms.get(subscription.project_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            del self._rooms[subscription.project_id]

    def publish(self, project_id: uuid.UUID, *, type: str, data: dict[str, Any]) -> int:
        """Fan an event out to everyone in the room. Returns its seq.

        Deliberately synchronous and non-blocking: it is called from
        request handlers right after a commit, and one unresponsive
        client must never be able to slow down -- let alone stall -- the
        request that produced the event.

        The sequence advances even with nobody connected, so a client that
        joins later and compares against the snapshot sees a consistent
        number rather than one that only counts events it happened to be
        present for.
        """
        seq = self._seq.get(project_id, 0) + 1
        self._seq[project_id] = seq
        envelope = {"seq": seq, "type": type, "data": data}

        for subscription in list(self._rooms.get(project_id, ())):
            self._deliver(subscription, envelope)
        return seq

    def _deliver(self, subscription: RoomSubscription, envelope: Any) -> None:
        """Queue one item -- an envelope, or the CLOSE sentinel."""
        try:
            subscription.queue.put_nowait(envelope)
            return
        except asyncio.QueueFull:
            pass

        # The client has stopped reading. Drop its oldest event to make
        # room for the close sentinel and end the connection, rather than
        # discarding this one silently: the documented recovery is
        # reconnect-and-refetch, and a client that is quietly missing
        # events has no way to know it should.
        subscription.overflowed = True
        try:
            subscription.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - full then empty
            pass
        try:
            subscription.queue.put_nowait(CLOSE)
        except asyncio.QueueFull:  # pragma: no cover - space was just made
            pass
        logger.warning(
            "room subscriber fell behind; closing",
            extra={"project_id": str(subscription.project_id)},
        )

    def close_all(self) -> None:
        """End every send loop. Called at shutdown.

        Without this, lifespan teardown waits on socket tasks that are
        blocked reading a queue nothing will ever fill again.
        """
        for subscribers in list(self._rooms.values()):
            for subscription in list(subscribers):
                self._deliver(subscription, CLOSE)


@lru_cache
def get_room_events() -> RoomEventBus:
    """The process-wide bus, cached like get_storage()/get_default_planner().

    One instance per process is the point -- a registry that request
    handlers write to and socket tasks read from only works if both reach
    the same object.
    """
    return RoomEventBus()
