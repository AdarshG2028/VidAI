"""Finds a video's most recent edited output, so a later proposal can build
on it instead of always compiling against the original upload.

The bug this exists to fix: a room asked to "trim 0:40-1:10", approved it,
then asked to "trim 0:10-0:15" -- and the second trim silently ran on the
untouched original, because nothing anywhere recorded what the first trim
produced. `Video.storage_uri` is set once at upload and never reassigned
(see backend/models/video.py); every proposal re-resolved handles straight
back to it.

**No new table.** `workflow_compiler.compile_workflow` already stamps
`payload["stage_params"]["0"]["video_ids"]` with the real `Video.id`
behind each handle it compiled (`ExecutionContext.video_db_ids`), and
`room_snapshot_service.export_artifacts` already knows how to pick a job's
real, non-preview, completed output apart from noise -- including
excluding previews, which must never advance what a later edit builds on
(a preview is deliberately fast and low-resolution; chaining onto one
would make every subsequent export degrade, silently). This module reuses
both rather than persisting a second copy of either.
"""

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.repositories.project_job_repository import ProjectJobRepository
from backend.repositories.result_repository import ResultRepository
from backend.services.room_snapshot_service import export_artifacts, final_result_by_job
from backend.storage import get_storage
from backend.workers.media import (
    AssetKind,
    extract_video_metadata,
    materialize_to_tempfile,
    primary_video,
    probe,
)

logger = logging.getLogger(__name__)

__all__ = [
    "VideoAnalysis",
    "VideoEdit",
    "analysis_assets",
    "measure",
    "recent_edits",
    "summarize_analysis",
]


@dataclass(frozen=True)
class VideoEdit:
    uri: str
    job_id: uuid.UUID
    edited_at: dt.datetime
    # The producing job's stage names, e.g. ["remove_segment", "trim"] --
    # what with_edit_history tells the planner is ALREADY reflected in this
    # handle, so an incremental request doesn't re-propose them on top of
    # an already-edited video and double-apply them (the bug this field
    # exists to let the prompt warn about: "remove the last 5 secs of this
    # too" re-including remove_segment cut a second, different 30s out of
    # an already-cut clip instead of just trimming the extra 5s).
    stages: list[str]


async def recent_edits(
    session: AsyncSession, project_id: uuid.UUID, video_id: uuid.UUID, *, limit: int = 2
) -> list[VideoEdit]:
    """This video's most recent completed, non-preview, single-video edits,
    newest first, capped at `limit`. [] if it has never been through one.

    A bounded window, not a full version history: the room can revert to
    the latest edit, the one before it, or the original -- not to an
    arbitrary point further back. That is a product choice (asked for
    exactly as "root and last two"), not a technical ceiling; raising
    `limit` returns a longer window without any other change here.

    **"Single-video" is deliberate.** A job only counts as one of this
    video's versions when stage 0 named exactly this one video and nothing
    else -- a merge, which names two, produces no single video's "next
    version", and this must never guess which of the two it was.
    Restricting to stage 0 is safe: it is the only stage a job's video_ids
    are ever load-bearing for (every later stage takes its real input from
    the prior stage's output, see backend/workers/media.py
    resolve_input_uri), so stage 0 is where "what did this job start from"
    is answered.

    `list_completed_jobs` is already ordered newest-completed-first (the
    same ordering the room's version list depends on), so collecting
    matches in that order and stopping at `limit` is already newest-first.
    """
    completed = await ProjectJobRepository(session).list_completed_jobs(project_id)
    if not completed:
        return []

    results_by_job = final_result_by_job(
        await ResultRepository(session).list_by_jobs([job.id for job in completed])
    )

    target = str(video_id)
    found: list[VideoEdit] = []
    for job in completed:
        if len(found) >= limit:
            break
        stage_zero = (job.payload or {}).get("stage_params", {}).get("0", {})
        if stage_zero.get("video_ids") != [target]:
            continue
        # export_artifacts is [] for a preview or an unfinished job.
        video_asset = primary_video(export_artifacts(job, results_by_job.get(job.id)))
        if video_asset is None:
            continue
        # ...but it is NOT empty for an analysis-only job, which is what
        # this check exists for. transcribe/detect_scenes/detect_filler_words
        # pass the video through untouched, and their _finish() inserts an
        # Asset(kind=VIDEO, uri=<the input>) when nothing was carried -- so
        # a lone `detect_scenes` run produces a perfectly real video asset
        # and used to be recorded here as an "edit" whose uri is the
        # unedited original. That burned one of the two version slots and
        # told the planner "already applied: detect_scenes" about a video
        # nothing had edited.
        #
        # The honest test is whether the job produced something *different*
        # from what it started with, so compare against the URI stage 0 was
        # compiled with rather than trusting the asset list to be empty.
        if video_asset.uri in _stage_zero_input_uris(stage_zero):
            continue
        found.append(
            VideoEdit(
                uri=video_asset.uri,
                job_id=job.id,
                edited_at=job.completed_at or job.created_at,
                stages=list(job.workflow.get("workflow", [])),
            )
        )
    return found


@dataclass(frozen=True)
class VideoAnalysis:
    """One analysis output a completed job left behind for this video --
    scenes, a transcript, filler words. The payload itself stays behind
    `uri`; see summarize_analysis for turning it into prompt-safe facts."""

    kind: str
    uri: str
    job_id: uuid.UUID
    produced_at: dt.datetime


async def analysis_assets(
    session: AsyncSession, project_id: uuid.UUID, video_id: uuid.UUID
) -> list[VideoAnalysis]:
    """Every non-video asset this video's completed jobs have produced,
    newest first, one per kind.

    Deliberately NOT part of recent_edits. That function's `limit=2` and
    its primary_video filter are what `_previous`/`_original` mean, and the
    analysis a planner needs is routinely older than the last two edits --
    a transcript produced five edits ago is still the transcript. So this
    walks the whole completed history and keeps the newest of each kind.

    **Newest wins (supersede, not accumulate).** Re-running detect_scenes at
    a different threshold replaces the older scene list rather than sitting
    alongside it; the planner is being told what is currently known about a
    video, not offered a history to compare. Changing that would mean
    keeping every (kind, job) pair and teaching the prompt to distinguish
    them, which nothing needs yet.

    Previews are excluded for free -- export_artifacts already drops them,
    the same reason recent_edits can trust it.
    """
    completed = await ProjectJobRepository(session).list_completed_jobs(project_id)
    if not completed:
        return []

    results_by_job = final_result_by_job(
        await ResultRepository(session).list_by_jobs([job.id for job in completed])
    )

    target = str(video_id)
    newest_by_kind: dict[str, VideoAnalysis] = {}
    for job in completed:  # already newest-completed-first
        stage_zero = (job.payload or {}).get("stage_params", {}).get("0", {})
        if stage_zero.get("video_ids") != [target]:
            continue
        for asset in export_artifacts(job, results_by_job.get(job.id)):
            if asset.kind == AssetKind.VIDEO or asset.kind in newest_by_kind:
                continue
            newest_by_kind[asset.kind] = VideoAnalysis(
                kind=asset.kind,
                uri=asset.uri,
                job_id=job.id,
                produced_at=job.completed_at or job.created_at,
            )
    return list(newest_by_kind.values())


# How many individual timestamps a scene/filler-word summary names before
# it stops and reports a count instead. The planner needs enough to reason
# about where cuts fall, not the whole list -- an hour of footage can carry
# hundreds, and this text goes into every subsequent prompt for that room.
_MAX_LISTED_ITEMS = 10
_MAX_TRANSCRIPT_CHARS = 400


def summarize_analysis(analysis: VideoAnalysis) -> str:
    """Prompt-safe facts derived from an analysis asset's stored payload.

    Reads the object, so this costs one storage fetch per analysis per
    turn. Bounded on both sides: only the newest asset per kind is ever
    passed here (analysis_assets), and the text produced is capped.

    Never returns the raw payload. A transcript is unbounded text and a
    scene list can be hundreds of entries -- pasting either into a system
    prompt would blow the context budget on data the planner mostly needs
    the *shape* of.

    Degrades rather than raising: a summary is an enrichment, and a missing
    or unreadable object must not break the chat turn that asked for it --
    the same posture measure() takes above.
    """
    try:
        raw = get_storage().get(analysis.uri)
        data = json.loads(raw)
    except Exception:
        logger.warning(
            "could not read analysis asset for chat context",
            extra={"uri": analysis.uri, "kind": analysis.kind},
            exc_info=True,
        )
        return f"{analysis.kind}: available (contents could not be read just now)"

    if analysis.kind == AssetKind.SCENES:
        cuts = data.get("cuts", []) if isinstance(data, dict) else []
        # The parameter the analysis ran with, carried through so the
        # planner can tell a coarse pass from a fine one -- and so that
        # "find more cuts than that" is a request it can act on by
        # re-running at a different threshold rather than guessing.
        threshold = data.get("threshold") if isinstance(data, dict) else None
        label = f"scenes (threshold {threshold:g})" if isinstance(threshold, (int, float)) else "scenes"
        times = [c.get("time") for c in cuts if isinstance(c, dict) and c.get("time") is not None]
        if not times:
            return f"{label}: analysed, no cuts detected (one continuous shot)"
        listed = ", ".join(_timestamp(t) for t in times[:_MAX_LISTED_ITEMS])
        suffix = f" (first {_MAX_LISTED_ITEMS} of {len(times)})" if len(times) > _MAX_LISTED_ITEMS else ""
        return f"{label}: {len(times)} cuts at {listed}{suffix}"

    if analysis.kind == AssetKind.FILLER_WORDS:
        instances = data.get("instances", []) if isinstance(data, dict) else []
        if not instances:
            return "filler words: analysed, none found"
        times = [
            i.get("segment_start")
            for i in instances
            if isinstance(i, dict) and i.get("segment_start") is not None
        ]
        listed = ", ".join(_timestamp(t) for t in times[:_MAX_LISTED_ITEMS])
        suffix = f" (first {_MAX_LISTED_ITEMS} of {len(times)})" if len(times) > _MAX_LISTED_ITEMS else ""
        return f"filler words: {len(instances)} found{f' at {listed}{suffix}' if times else ''}"

    if analysis.kind == AssetKind.CONTENT_MATCHES:
        # Carries the query verbatim, which is the whole reason this feature
        # needs no planner-side analyze loop: when a search finds nothing,
        # the room reads *what was searched for* on the next turn and can
        # rephrase it, instead of the planner silently retrying on its own.
        query = data.get("query") if isinstance(data, dict) else None
        matches = data.get("matches", []) if isinstance(data, dict) else []
        quoted = f' for "{query}"' if isinstance(query, str) and query else ""
        if not matches:
            return (
                f"content search{quoted}: ran, found nothing on screen "
                "-- a different description may work better"
            )
        ranges = [
            f"{_timestamp(m['start'])}-{_timestamp(m['end'])}"
            for m in matches
            if isinstance(m, dict) and m.get("start") is not None and m.get("end") is not None
        ]
        listed = ", ".join(ranges[:_MAX_LISTED_ITEMS])
        suffix = f" (first {_MAX_LISTED_ITEMS} of {len(ranges)})" if len(ranges) > _MAX_LISTED_ITEMS else ""
        return f"content search{quoted}: {len(ranges)} ranges at {listed}{suffix}"

    if analysis.kind == AssetKind.TRANSCRIPT:
        text = data.get("text", "") if isinstance(data, dict) else ""
        if not isinstance(text, str) or not text.strip():
            return "transcript: available (empty)"
        words = len(text.split())
        excerpt = text.strip()[:_MAX_TRANSCRIPT_CHARS]
        ellipsis = "…" if len(text.strip()) > _MAX_TRANSCRIPT_CHARS else ""
        return f'transcript: {words} words, begins "{excerpt}{ellipsis}"'

    return f"{analysis.kind}: available"


def _timestamp(seconds: float) -> str:
    """m:ss -- what a user says, and what trim/remove_segment params look
    like in the same conversation."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _stage_zero_input_uris(stage_zero: dict[str, Any]) -> frozenset[str]:
    """What stage 0 was compiled to read, per workflow_compiler (which
    stamps `video_uris` alongside `video_ids`). Empty when absent, so a
    payload predating that convention degrades to the old behaviour rather
    than silently discarding every edit."""
    uris = stage_zero.get("video_uris") or []
    return frozenset(uri for uri in uris if isinstance(uri, str))


async def measure(uri: str) -> dict[str, Any] | None:
    """duration/resolution/orientation/fps for a stored video, measured
    fresh via ffprobe right now -- None if that fails for any reason.

    Why this exists: `VideoContext.duration_seconds` otherwise only ever
    carries the ORIGINAL upload's upload-time analysis, which is wrong for
    an edited handle -- a room that trims a clip and is then asked "how
    long is it now" had nothing but a stale number and a warning that it
    "may not be accurate," which is exactly what pushed a real
    conversation into asking the room for the duration in chat, getting a
    correct answer, misreading which duration it was an answer to, and
    then hallucinating one entirely on the turn after that. Grounding the
    edited handle's facts in a real measurement removes the need to guess.

    Broad except is deliberate: this only ever enriches a chat turn or a
    proposal compile with better facts, so a probe failure (missing file,
    ffprobe crashing, an unreachable storage backend) must degrade to "no
    fresher facts than before," never break the turn that asked for them
    -- the same reasoning backend/services/job_progress_poller.py uses for
    swallowing a failed poll rather than taking the whole room down over it.
    """
    try:
        with materialize_to_tempfile(uri) as path:
            probe_data = await probe(path)
        return extract_video_metadata(probe_data)
    except Exception:
        logger.warning("could not measure video for chat context", extra={"uri": uri}, exc_info=True)
        return None
