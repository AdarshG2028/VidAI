"""PlannerContext (Changelog v8) -- everything a planning call needs,
assembled once by ConversationService before invoking the planner. Replaces
passing "many unrelated arguments" into Planner.respond; the planner never
fetches data itself (project/conversation/videos/preferences all come in
already loaded).
"""

import uuid
from dataclasses import dataclass, field, replace

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Message, Project, UserPreference, Video
from backend.services.capability_registry import CapabilityRegistry
from backend.services.video_chain import (
    analysis_assets,
    measure,
    recent_edits,
    summarize_analysis,
)


@dataclass(frozen=True)
class VideoContext:
    """One of the project's videos as the planner sees it: a short,
    LLM-facing handle (never a raw Video.id -- models are unreliable at
    echoing UUIDs verbatim) plus enough to describe it in a prompt."""

    handle: str
    video_id: str
    display_name: str

    # Measured by the video_analysis worker on upload (§8) and stored on
    # its Result. Optional because analysis may still be running, or may
    # have failed, and a planner that can still propose *something* is
    # better than one that refuses.
    #
    # Without these the planner is blind to facts the system already
    # knows: asked to "trim the last 10 seconds" it has to ask how long
    # the video is, and asked to "make it vertical" it cannot tell that it
    # already is.
    duration_seconds: float | None = None
    resolution: str | None = None
    orientation: str | None = None

    # What this handle actually resolves to -- the original upload's URI
    # by default. with_edit_history() below is what ever sets this to
    # something else. Defaults to "" (not None) so a caller that only
    # needs the metadata fields, like every existing test constructing a
    # VideoContext by hand, is unaffected.
    uri: str = ""

    # Set by with_edit_history() when this handle's uri points at a prior
    # edit's output rather than the original upload -- told to the planner
    # so it knows to say so, and so it knows the facts above may be stale.
    edit_note: str | None = None

    # Prompt-ready one-liners for analysis this room has already run on
    # this video ("scenes: 6 cuts at 0:00, 0:12, ...", "transcript: 412
    # words, begins ..."). Empty until with_edit_history fills it.
    #
    # Analysis assets otherwise travel only *within* one compiled job
    # (media.forward_assets), so before this the planner could propose
    # detect_scenes, watch the room approve it, and on the very next turn
    # have no idea it had ever run -- let alone what it found. Defaulted so
    # every VideoContext built by hand elsewhere is unaffected.
    analysis: tuple[str, ...] = ()


def build_video_contexts(
    videos: list[Video], analysis: dict[str, dict] | None = None
) -> list[VideoContext]:
    """Assigns stable handles ("video_1", "video_2", ...) in the given
    order. Callers must pass videos in a consistent order (VideoRepository
    .list_by_project already orders by created_at) -- confirm-proposal
    re-derives the same handle -> uri mapping later by calling this again,
    so a proposal's video_ids only resolve correctly if the project's video
    list hasn't changed shape in between. Acceptable for V1 per the
    "no proposal persistence in Phase 4" deferral; revisit if that
    assumption ever breaks in practice.

    Deliberately pure (no DB besides the `videos`/`analysis` already
    fetched) -- see with_edit_history() for the async step that looks up
    whether any of these videos have since been edited.
    """
    analysis = analysis or {}
    return [
        VideoContext(
            handle=f"video_{i}",
            video_id=str(video.id),
            display_name=video.name or video.original_filename,
            duration_seconds=analysis.get(str(video.id), {}).get("duration_seconds"),
            resolution=analysis.get(str(video.id), {}).get("resolution"),
            orientation=analysis.get(str(video.id), {}).get("orientation"),
            uri=video.storage_uri,
        )
        for i, video in enumerate(videos, start=1)
    ]


def _edit_note(stages: list[str], *, measured: bool) -> str:
    """What the planner is told about an edited handle: that it's already
    the output of specific prior stages, and what NOT to do about that.

    Naming the stages is what this exists for. A note that only says "this
    is a previous edit" (the earlier version of this function) gave the
    model nothing concrete to avoid repeating -- and in a real room, asked
    to remove an extra 5 seconds from an already-trimmed clip, it re-emitted
    the *entire* prior workflow including the original remove_segment call.
    That ran a second, different 30-second cut on top of the already-cut
    video instead of just trimming 5 more seconds off the end -- 105.8s
    minus a second unwanted 30s cut is 75.8s, which is exactly the wrong
    output the room got. Naming "already applied: remove_segment, trim" and
    saying not to repeat them is the direct fix for that failure mode.
    """
    applied = ", ".join(stages) if stages else "a prior edit"
    if measured:
        facts_clause = (
            "the duration/resolution above were measured for this edited "
            "version, not carried over from the original"
        )
    else:
        facts_clause = (
            "its duration/resolution could not be measured just now, so the "
            "facts above are the ORIGINAL video's and may not be accurate for it"
        )
    return (
        f"this is the OUTPUT of a previous edit in this room (already applied: "
        f"{applied}) -- {facts_clause}. For an ADDITIONAL change, propose only "
        f"the new stage(s) on top of this -- do NOT include {applied} again, "
        f"that would re-run them on the already-edited video and compound the "
        f"edit. To instead CHANGE how one of those stages was done, use the "
        f"matching `_previous`/`_original` handle and redo from there, not this one."
    )


async def _measured_facts(
    uri: str, fallback: VideoContext, stages: list[str]
) -> tuple[float | None, str | None, str | None, str]:
    """(duration_seconds, resolution, orientation, edit_note) for `uri`,
    freshly measured -- or `fallback`'s own (stale) facts if measuring
    fails, so a probe hiccup degrades gracefully instead of blanking out
    what little was already known."""
    facts = await measure(uri)
    if facts is None:
        return (
            fallback.duration_seconds,
            fallback.resolution,
            fallback.orientation,
            _edit_note(stages, measured=False),
        )
    return (
        facts.get("duration_seconds"),
        facts.get("resolution"),
        facts.get("orientation"),
        _edit_note(stages, measured=True),
    )


async def with_edit_history(
    session: AsyncSession, project_id: uuid.UUID, contexts: list[VideoContext]
) -> list[VideoContext]:
    """For every handle whose video has been edited, point its uri at the
    latest edit (with freshly measured duration/resolution/orientation,
    not the original's) and add up to two more handles to revert to:
    `<handle>_previous` (the edit before that, if one exists) and
    `<handle>_original` (the untouched upload) -- a bounded 3-state window
    (latest / previous / original), not a full version history, per the
    room being able to revert to either of the last two edits or start
    over, and nothing further back than that.

    Building on the latest edit by default is what makes "trim 0:10 to
    0:15" after "trim 0:40 to 1:10" continue from the first trim instead of
    silently re-running on the original. Measuring it fresh rather than
    reusing the original's stale facts is what stops the planner having to
    ask the room for the current duration in chat -- fragile in a way a
    real conversation demonstrated: an answer meant as the post-edit
    duration got read as the original's, and the turn after that
    fabricated a duration nobody had ever stated.

    **The one place this is resolved.** Both ConversationService (building
    what the planner sees) and ProposalConfirmationService (compiling an
    approved proposal into a job) call this on top of build_video_contexts.
    If they resolved edit history separately, a proposal validated against
    one view could 409 at approval against the other -- so there is
    exactly one function that decides which handles exist and what they
    point at, and both callers inherit it.
    """
    augmented: list[VideoContext] = []
    for context in contexts:
        video_uuid = uuid.UUID(context.video_id)
        # What this room already knows about the video's *content*, as
        # opposed to its geometry. Attached to the primary handle only:
        # analysis describes the underlying footage, and repeating it on
        # `_previous`/`_original` would just spend context restating it.
        analysis = tuple(
            summarize_analysis(item)
            for item in await analysis_assets(session, project_id, video_uuid)
        )
        edits = await recent_edits(session, project_id, video_uuid, limit=2)
        if not edits:
            augmented.append(replace(context, analysis=analysis) if analysis else context)
            continue

        duration, resolution, orientation, edit_note = await _measured_facts(
            edits[0].uri, context, edits[0].stages
        )
        augmented.append(
            replace(
                context,
                uri=edits[0].uri,
                duration_seconds=duration,
                resolution=resolution,
                orientation=orientation,
                edit_note=edit_note,
                analysis=analysis,
            )
        )

        if len(edits) >= 2:
            prev_duration, prev_resolution, prev_orientation, prev_edit_note = (
                await _measured_facts(edits[1].uri, context, edits[1].stages)
            )
            augmented.append(
                VideoContext(
                    handle=f"{context.handle}_previous",
                    video_id=context.video_id,
                    display_name=(
                        f"{context.display_name} (previous edit, before the most recent one)"
                    ),
                    duration_seconds=prev_duration,
                    resolution=prev_resolution,
                    orientation=prev_orientation,
                    uri=edits[1].uri,
                    edit_note=prev_edit_note,
                )
            )

        augmented.append(
            VideoContext(
                handle=f"{context.handle}_original",
                video_id=context.video_id,
                display_name=f"{context.display_name} (original, before edits)",
                duration_seconds=context.duration_seconds,
                resolution=context.resolution,
                orientation=context.orientation,
                uri=context.uri,
            )
        )
    return augmented


@dataclass(frozen=True)
class PlannerContext:
    project: Project
    conversation_history: list[Message]
    videos: list[VideoContext]
    preferences: UserPreference | None
    capability_registry: CapabilityRegistry
    # The room's approval policy, rendered so the planner can *describe*
    # what happens next ("waiting for approval from all team members").
    # It never decides outcomes -- that is the collaboration layer's job --
    # so this is wording only (Phase 9a).
    approval_policy: str | None = field(default=None)


def participant_handles(history: list[Message]) -> dict[uuid.UUID, str]:
    """Stable, short labels for the humans in a conversation (Phase 9a).

    There is no users table -- a sender is a bare asserted UUID -- so the
    planner has no names to work with, and a raw UUID is both unreadable
    and something models echo unreliably. The same reasoning produced
    `video_1` handles in Phase 4, and this follows it: numbered in order of
    first appearance, so a given member keeps one label for the whole
    transcript and "member_2 disagreed with member_1" is a statement the
    model can actually make.

    Assistant turns are excluded: they have no sender, and the planner
    knows which turns are its own from the chat role.
    """
    handles: dict[uuid.UUID, str] = {}
    for message in history:
        if message.sender_id is None or message.sender_id in handles:
            continue
        handles[message.sender_id] = f"member_{len(handles) + 1}"
    return handles
