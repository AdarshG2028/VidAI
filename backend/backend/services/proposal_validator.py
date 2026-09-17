"""Validates a Proposal against a CapabilityRegistry (§6), before it's ever
compiled into a Setu job.

Collects every error found rather than stopping at the first: Phase 4's
regenerate-and-retry loop feeds the full ValidationResult back into the
planner's prompt, and "unknown stage 'crp', unknown param 'britness' on
'color'" is a better retry signal than one error at a time.

Everything checked here is checked *before* a job exists, which is the
point: a proposal the planner can still fix should fail in this loop,
not halfway through execution where the only outcome left is a DLQ'd job
and a user with no output. That's why the asset-availability scan below
lives here rather than being discovered by a worker at runtime.
"""

from dataclasses import dataclass, field

from backend.services.capability_registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
)
from backend.services.proposal import Proposal


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _matches_type(value: object, expected_type: type) -> bool:
    """Type check for a param, tolerant of how JSON represents numbers.

    A plain isinstance() is wrong at this boundary in two directions:

    - JSON has a single number type, so a planner writes `1` as readily as
      `1.0` for a float param -- and `isinstance(1, float)` is False. That
      would reject a perfectly good brightness of 1 and send the planner
      round the regenerate loop over nothing.
    - Python's bool is a subclass of int, so `isinstance(True, int)` is
      True and a param declared int would silently accept `true`.

    Both are handled here rather than by loosening the declared schemas,
    which stay the honest description of what a capability wants.
    """
    if expected_type is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected_type)


def validate_proposal(
    proposal: Proposal,
    registry: CapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
    *,
    known_video_handles: frozenset[str] | None = None,
) -> ValidationResult:
    if not proposal.workflow:
        return ValidationResult(valid=False, errors=["workflow must not be empty"])

    errors: list[str] = []

    # Seeded with "video" because every workflow starts from an uploaded
    # video -- the first stage has one available without any predecessor
    # having produced it.
    #
    # Walked in order and only ever added to, mirroring the monotonic
    # accumulation workers actually perform (media.forward_assets): a
    # stage passes on everything it received, so an asset produced at
    # stage 1 is still available at stage 5 with unrelated stages in
    # between. That's what makes `transcribe -> color -> burn_subtitles`
    # valid, and why this can't be a simple "is the previous stage the
    # right one" adjacency check.
    available_kinds: set[str] = {"video"}

    for index, item in enumerate(proposal.workflow):
        capability = registry.get(item.stage)
        if capability is None:
            errors.append(f"unknown stage '{item.stage}'")
            continue

        missing = [
            kind for kind in capability.requires_asset_kinds if kind not in available_kinds
        ]
        if missing:
            errors.append(
                f"stage '{item.stage}' (position {index}) needs "
                f"{', '.join(f'{kind!r}' for kind in missing)} but nothing before it "
                f"produces that"
            )
        available_kinds.update(capability.produces_asset_kinds)

        # Structural, not asset-kind-based: a stage like merge that needs
        # every project video's real URI (not just "a video") only has
        # that at position 0 -- every later stage's real input is whatever
        # the stage before it produced, not the project's uploads. Caught
        # here rather than left to the worker's own runtime refusal, so
        # the room votes on a proposal that can actually succeed instead
        # of approving one already guaranteed to dead-letter.
        if capability.must_be_first_stage and index != 0:
            errors.append(
                f"stage '{item.stage}' (position {index}) must be the first stage "
                f"of the workflow -- move it to position 0, with any other stages "
                f"after it"
            )

        if known_video_handles is not None:
            # A stage gets its input either from the stage before it or,
            # when nothing precedes it, from the video it names. So the
            # first stage of a workflow must name one -- an empty
            # video_ids there passes every other check and then dies at
            # execution time with "no input video", which is precisely
            # the failure this validator exists to convert into something
            # the planner can fix. Observed live: a proposal for
            # [crop, render] with no video_ids validated cleanly and
            # dead-lettered.
            #
            # Gated on known_video_handles because that is what tells us
            # a project's videos were actually resolvable; callers with
            # no video context (Phase 3's hand-authored proposals) are
            # unaffected.
            if index == 0 and not item.video_ids and "video" in capability.requires_asset_kinds:
                errors.append(
                    f"stage '{item.stage}' is first in the workflow and must name the "
                    f"video it applies to in video_ids"
                )
            for video_id in item.video_ids:
                if video_id not in known_video_handles:
                    errors.append(
                        f"unknown video handle '{video_id}' for stage '{item.stage}'"
                    )

        for key, value in item.params.items():
            if key not in capability.parameter_schema:
                errors.append(f"unknown param '{key}' for stage '{item.stage}'")
                continue
            expected_type = capability.parameter_schema[key]
            if not _matches_type(value, expected_type):
                errors.append(
                    f"param '{key}' for stage '{item.stage}' must be "
                    f"{expected_type.__name__}, got {type(value).__name__}"
                )

    return ValidationResult(valid=not errors, errors=errors)
