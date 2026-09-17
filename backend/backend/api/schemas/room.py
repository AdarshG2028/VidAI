"""The room snapshot (Phase 8) -- everything a client needs to render a
project in one request.

Deliberately composed out of the existing per-resource schemas rather
than flattened into new ones: a video here is the same `VideoSummaryResponse`
that `GET /projects/{id}/videos` returns, a message the same
`MessageResponse`, a job the same `JobResponse`. A client that already
renders those does not learn a second shape for the same thing, and a
field added to one is added to both.

This is also the WebSocket reconnect path (architecture doc, Phase 8):
refetch the snapshot, then resume the stream. When the socket lands it
gains a `seq` field here so a client can tell where the snapshot sits in
the event stream -- additive, and not built yet because there is no
sequence to report until there is a socket producing one.
"""

import datetime as dt
import uuid

from pydantic import BaseModel

from backend.api.schemas.artifact import ArtifactResponse
from backend.api.schemas.conversation import MessageResponse
from backend.api.schemas.job import JobResponse
from backend.api.schemas.member import MemberResponse
from backend.api.schemas.project import ProjectResponse
from backend.api.schemas.video import VideoSummaryResponse


class ExportResponse(BaseModel):
    """A finished piece of work the room can download.

    Carries only the *final* stage's assets, not every stage's. Two
    reasons: the final stage is what "the export" means, and
    artifact_cleanup_service sweeps intermediates once a job has been
    finished longer than the retention window -- so older exports would
    otherwise render as a list of empty stages. Per-stage inspection
    stays at GET /jobs/{id}/artifacts, which is the view built for it.
    """

    job_id: uuid.UUID
    workflow: list[str]
    completed_at: dt.datetime | None
    artifacts: list[ArtifactResponse]


class RoomSnapshotResponse(BaseModel):
    # Where this snapshot sits in the room's event stream. A client that
    # connects the socket first, then fetches this, discards any event
    # whose seq is <= this value -- which closes the handoff window
    # without needing the server to replay anything. Resets to zero when
    # the process restarts; harmless, because every reconnect re-baselines
    # against a fresh snapshot.
    seq: int
    project: ProjectResponse
    members: list[MemberResponse]
    videos: list[VideoSummaryResponse]
    messages: list[MessageResponse]
    # Anything not in a terminal state, so a client can show progress
    # without having tracked the job id from whichever request started it
    # -- which is the whole point in a room, where the member watching is
    # often not the member who submitted.
    active_jobs: list[JobResponse]
    # Terminal work that produced no version: cancelled or dead-lettered.
    # In neither list on either side of it, which is why it needs its own
    # -- a job a member deliberately cancelled must not simply disappear.
    ended_jobs: list[JobResponse]
    # Newest first. The derived version list; see ExportResponse.
    exports: list[ExportResponse]
