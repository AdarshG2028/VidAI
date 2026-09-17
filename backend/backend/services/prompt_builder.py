"""PromptBuilder (Changelog v8) -- PlannerContext -> Prompt, extracted from
the planner so prompt engineering stays separate from the LLM call itself.
The same `build` (with an optional validation-feedback turn appended) is
reused for both the first attempt and Phase 4's one semantic retry; future
prompt types (clarification, revision, summaries) can add their own
sections here without duplicating context assembly.
"""

from dataclasses import dataclass

from backend.models import MessageRole
from backend.services.planner_context import PlannerContext, participant_handles


@dataclass(frozen=True)
class Prompt:
    system: str
    messages: list[dict[str, str]]


_ROLE_TO_CHAT_ROLE = {
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
}


# Python type names mean nothing to a planner emitting JSON; these are the
# names it actually reasons about.
_JSON_TYPE_NAMES = {str: "string", int: "integer", float: "number", bool: "boolean"}

# How each stored preference reads to the planner. Phrased as a fact about
# the user rather than a column name, since that is what the model reasons
# over.
_PREFERENCE_LABELS = (
    ("preferred_platform", "publishes to"),
    ("preferred_export_format", "wants exports as"),
    ("preferred_resolution", "wants resolution"),
    ("subtitle_language", "wants subtitles in"),
)


def _video_facts(video: object) -> str:
    """What analysis measured about this video, appended to its line.

    Rendered inline rather than as a separate block so a handle and its
    facts can never drift apart in a multi-video project. Duration is
    given in seconds because every time-based capability (trim) takes
    seconds, so the planner needs no conversion to use it.
    """
    facts = []
    duration = getattr(video, "duration_seconds", None)
    if duration:
        facts.append(f"{duration:g}s long")
    if resolution := getattr(video, "resolution", None):
        orientation = getattr(video, "orientation", None)
        facts.append(f"{resolution}{f' {orientation}' if orientation else ''}")
    return f" ({', '.join(facts)})" if facts else ""


def _render_preferences(preferences: object | None) -> list[str]:
    """Standing preferences, as lines the planner can actually read.

    This used to interpolate the ORM object directly, which rendered as
    `<UserPreference object at 0x...>` -- the model was being handed a
    Python repr and, unsurprisingly, ignored it. The bug survived because
    nothing wrote preferences until Phase 6, so every prior run had none
    and the branch never produced anything meaningful.

    Only fields that are actually set are rendered: a line saying a
    preference is unset is noise that invites the model to treat absence
    as a decision.
    """
    if preferences is None:
        return []

    facts = [
        f"- {label}: {value}"
        for field, label in _PREFERENCE_LABELS
        if (value := getattr(preferences, field, None))
    ]
    if getattr(preferences, "captions_enabled", False):
        facts.append("- always wants captions burned in")

    if not facts:
        return []
    return [
        "What this user has told you before, in earlier sessions. Apply these "
        "unless the current request says otherwise, and do not ask about them "
        "again:",
        *facts,
    ]


def _attributed(message, handles: dict) -> str:
    """One transcript turn, labelled with who said it and when.

    Only user turns are labelled. The planner's own turns need no speaker
    -- the chat role already says so -- and prefixing them would teach the
    model to emit the prefix back inside its JSON.
    """
    if message.sender_id is None:
        return message.content
    handle = handles.get(message.sender_id, "member")
    # Clock time, not a date: what matters to facilitation is the order and
    # rough spacing of turns within a discussion, and a full timestamp
    # spends tokens to say less.
    stamp = message.created_at.strftime("%H:%M") if message.created_at else ""
    return f"{handle} ({stamp}): {message.content}" if stamp else f"{handle}: {message.content}"


# Only rendered once a room actually has more than one participant. A solo
# user does not need to be told to watch for disagreement, and telling
# them to summarise "the discussion" invites a summary of a monologue.
_FACILITATION = (
    "More than one person is in this room. You are facilitating their "
    "discussion, not taking orders from whoever spoke last.",
    "- When members want incompatible things, say so plainly, name who "
    "wants what, and ask the room to decide. Do NOT propose a workflow "
    "that silently picks a side.",
    "- When they agree, or when one concedes, treat that as settled and "
    "move to a proposal.",
    "- Never invent agreement that has not happened.",
    "",
    "When you do propose, also fill in:",
    "- `reasoning`: why this workflow, in one or two sentences a "
    "non-technical member would follow.",
    "- `discussion_summary`: what the room actually decided and what was "
    "rejected along the way, so somebody arriving late can catch up.",
)


def _policy_lines(policy: str, collaborative: bool) -> list[str]:
    """How to *describe* what happens after a proposal -- wording only.

    The planner never decides whether a proposal is approved; the
    collaboration layer collects the votes. But a facilitation message
    that says "I'll start rendering" when the room actually has to vote
    first would be a lie the model told with no way of knowing better, so
    it is told what the room's rule is.
    """
    lines = [f"Approval policy for this room: {policy}"]
    if not collaborative:
        return lines
    if policy == "admin":
        lines.append(
            "A proposal runs once the project owner approves it. Say it is "
            "ready for the owner's approval -- never that it is running."
        )
    else:
        lines.append(
            "A proposal runs only once EVERY active member approves it, and "
            "a single rejection ends it. Say it is waiting for approval from "
            "all team members -- never that it is running, and never that a "
            "majority is enough."
        )
    lines.append(
        "Previews are exempt: any member may preview a pending proposal "
        "without a vote."
    )
    return lines


class PromptBuilder:
    def build(
        self, context: PlannerContext, *, validation_feedback: list[str] | None = None
    ) -> Prompt:
        system = self._build_system(context)
        # Attributed, not anonymous (Phase 9a). A shared room is a
        # conversation *between people*, and a planner that cannot tell
        # who said what cannot notice that two of them want opposite
        # things -- which is the single most important thing it has to
        # notice. Prefixed onto the content rather than carried in a
        # separate field because the chat format has nowhere else to put
        # it, and the model reads the text either way.
        handles = participant_handles(context.conversation_history)
        messages = [
            {
                "role": _ROLE_TO_CHAT_ROLE[m.role],
                "content": _attributed(m, handles),
            }
            for m in context.conversation_history
        ]
        if validation_feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous proposal was invalid for these reasons: "
                        + "; ".join(validation_feedback)
                        + ". Please produce a corrected proposal, or ask a clarifying "
                        "question if you're missing information needed to fix it."
                    ),
                }
            )
        return Prompt(system=system, messages=messages)

    def _build_system(self, context: PlannerContext) -> str:
        participants = participant_handles(context.conversation_history)
        lines = [
            "You are a video editing assistant. You never edit video yourself -- "
            "you either ask a clarifying question or propose a workflow of stages "
            "for a deterministic execution engine to run.",
            "",
            "Respond with `{\"type\": \"message\", ...}` if you need more information, "
            "or `{\"type\": \"proposal\", ...}` once you have enough to propose a "
            "concrete edit.",
            "",
            "Available videos in this project. Handles (video_1, video_2, ...) exist "
            "ONLY for the structured `video_ids` field -- that is the one place you "
            "must use the handle and never the display name. Everywhere you write "
            "prose for the room to read (`summary`, `reasoning`, `discussion_summary`, "
            "or a clarifying `text` message), do the opposite: use the video's "
            "display name (e.g. \"My Renamed Clip\"), never its handle -- the room "
            "named their own footage, and a reply that says \"video_1\" instead of "
            "the name they gave it reads as if you ignored them. When a handle's "
            "note says it is the output of a previous edit, build on THAT by "
            "default (e.g. a second trim request continues from the first) -- and "
            "when that note names which stages already ran, your workflow for an "
            "additional change must NOT include those stage names again: they are "
            "already baked into this "
            "handle, and repeating one re-runs it a second time on the already-"
            "edited video rather than adding the new change. A video with edit "
            "history may also list a `_previous` handle (the edit before the most "
            "recent one) and an `_original` handle (the untouched upload) -- use "
            "`_previous` only when the user asks to undo, go back a step, or build "
            "on the version before the last edit, and use `_original` only when "
            "they clearly ask to start over from scratch:",
        ]
        for video in context.videos:
            line = f"- {video.handle}: {video.display_name}{_video_facts(video)}"
            if video.edit_note:
                line += f" [{video.edit_note}]"
            lines.append(line)
            # Indented under the handle, for the same reason _video_facts
            # is inline: in a multi-video room an analysis line that
            # floated free of its handle would be worse than absent.
            for fact in getattr(video, "analysis", ()):
                lines.append(f"    already analysed -- {fact}")

        lines.append("")
        lines.append("Available stages (only use these in `workflow`):")
        for capability in context.capability_registry.list():
            lines.append(f"- {capability.name}: {capability.description}")
            # The parameter names must come from the schema, not from the
            # prose above. Before Phase 5 the only capability was `dummy`,
            # which takes none, so this was invisible; once capabilities
            # had real params the planner was left inferring key names from
            # the description -- which worked for `aspect_ratio` and
            # `brightness` by luck of phrasing, and failed for `rotate`,
            # where it asked the *user* what the parameter was called.
            if capability.parameter_schema:
                rendered = ", ".join(
                    f"{name} ({_JSON_TYPE_NAMES.get(type_, type_.__name__)})"
                    for name, type_ in sorted(capability.parameter_schema.items())
                )
                lines.append(f"  parameters: {rendered}")

        rendered_preferences = _render_preferences(context.preferences)
        if rendered_preferences:
            lines.append("")
            lines.extend(rendered_preferences)

        collaborative = len(participants) > 1
        if collaborative:
            lines.append("")
            lines.extend(_FACILITATION)

        if context.approval_policy is not None:
            lines.append("")
            lines.extend(_policy_lines(context.approval_policy, collaborative))

        return "\n".join(lines)
