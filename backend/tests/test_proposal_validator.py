"""Unit tests for validate_proposal (§6).

Uses a small fake registry, not DEFAULT_CAPABILITY_REGISTRY -- validate_proposal
takes the registry as a parameter specifically so these tests don't couple to
whatever's really registered in production (which grows every sub-phase of
Phase 5).
"""

from backend.services.capability_registry import CapabilityRegistry, StageCapability
from backend.services.proposal import Proposal, ProposalStage
from backend.services.proposal_validator import validate_proposal

_REGISTRY = CapabilityRegistry(
    {
        "crop": StageCapability(
            name="crop", description="crop", parameter_schema={"aspect_ratio": str}
        ),
        "color": StageCapability(
            name="color", description="color", parameter_schema={"brightness": float}
        ),
        # The 5F producer/consumer pair — the only capabilities whose asset
        # kinds diverge from the video-in/video-out default.
        "transcribe": StageCapability(
            name="transcribe",
            description="transcribe speech",
            produces_asset_kinds=("video", "transcript", "srt"),
        ),
        "burn_subtitles": StageCapability(
            name="burn_subtitles",
            description="burn captions in",
            requires_asset_kinds=("video", "srt"),
        ),
    }
)


def test_empty_workflow_is_rejected() -> None:
    proposal = Proposal(summary="do nothing", workflow=[])

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert "workflow must not be empty" in result.errors


def test_unregistered_stage_is_rejected() -> None:
    proposal = Proposal(
        summary="...", workflow=[ProposalStage(stage="nonexistent", params={})]
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert "unknown stage 'nonexistent'" in result.errors


def test_unknown_param_is_rejected() -> None:
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", params={"not_a_real_param": "x"})],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert "unknown param 'not_a_real_param' for stage 'crop'" in result.errors


def test_wrong_param_type_is_rejected() -> None:
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="color", params={"brightness": "very bright"})],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert "param 'brightness' for stage 'color' must be float, got str" in result.errors


def test_repeated_stage_is_allowed() -> None:
    """Reverses an earlier V1 policy (and the fix Changelog v8 planned for
    it). Under Phase 5's asset chaining a repeated stage is meaningful,
    not a hallucination: each instance operates on what the previous one
    produced, so `trim -> trim` is an ordinary "drop the intro, then the
    boring middle" workflow.

    The re-key that changelog proposed -- (stage, video_ids) -- would not
    have worked either: only stage 0's video_ids are real, since every
    later stage takes its input from previous_output, so two downstream
    trims would key identically anyway. Asset availability below is what
    guards proposal correctness now."""
    proposal = Proposal(
        summary="...",
        workflow=[
            ProposalStage(stage="crop", params={"aspect_ratio": "9:16"}),
            ProposalStage(stage="crop", params={"aspect_ratio": "1:1"}),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True
    assert result.errors == []


def test_multiple_errors_are_all_collected_not_just_the_first() -> None:
    proposal = Proposal(
        summary="...",
        workflow=[
            ProposalStage(stage="nonexistent", params={}),
            ProposalStage(stage="color", params={"brightness": "not a number"}),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert "unknown stage 'nonexistent'" in result.errors
    assert "param 'brightness' for stage 'color' must be float, got str" in result.errors
    assert len(result.errors) == 2


def test_unknown_video_handle_is_rejected_when_known_handles_given() -> None:
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", video_ids=["video_99"], params={"aspect_ratio": "9:16"})],
    )

    result = validate_proposal(proposal, _REGISTRY, known_video_handles=frozenset({"video_1"}))

    assert result.valid is False
    assert "unknown video handle 'video_99' for stage 'crop'" in result.errors


def test_known_video_handle_passes() -> None:
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "9:16"})],
    )

    result = validate_proposal(proposal, _REGISTRY, known_video_handles=frozenset({"video_1"}))

    assert result.valid is True


def test_video_handle_check_is_skipped_when_known_handles_not_given() -> None:
    """Callers that don't pass known_video_handles (e.g. Phase 3's
    hand-authored proposals with no PlannerContext) get the old behavior."""
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", video_ids=["anything"], params={"aspect_ratio": "9:16"})],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True


def test_valid_proposal_passes_with_no_errors() -> None:
    proposal = Proposal(
        summary="crop then brighten",
        workflow=[
            ProposalStage(stage="crop", params={"aspect_ratio": "9:16"}),
            ProposalStage(stage="color", params={"brightness": 1.1}),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True
    assert result.errors == []


# --- asset availability (Phase 5A) -----------------------------------------


def test_consumer_without_its_producer_is_rejected() -> None:
    """The failure class this check exists to prevent: burn_subtitles needs
    an srt, nothing produces one, and without this the proposal validates
    cleanly and then dies at execution time as a DLQ'd job with no output.
    Caught here instead, it becomes a ValidationResult the planner's
    regenerate-and-retry loop can actually fix."""
    proposal = Proposal(
        summary="add captions",
        workflow=[ProposalStage(stage="burn_subtitles")],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert any("burn_subtitles" in error and "srt" in error for error in result.errors)


def test_producer_then_consumer_is_accepted() -> None:
    proposal = Proposal(
        summary="transcribe and caption",
        workflow=[
            ProposalStage(stage="transcribe"),
            ProposalStage(stage="burn_subtitles"),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True
    assert result.errors == []


def test_producer_and_consumer_need_not_be_adjacent() -> None:
    """The property monotonic accumulation buys: an srt produced at stage 0
    is still available at stage 2 with an unrelated stage in between. An
    adjacency check would wrongly reject this."""
    proposal = Proposal(
        summary="transcribe, grade, then caption",
        workflow=[
            ProposalStage(stage="transcribe"),
            ProposalStage(stage="color", params={"brightness": 1.1}),
            ProposalStage(stage="burn_subtitles"),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True
    assert result.errors == []


def test_consumer_before_its_producer_is_rejected() -> None:
    """Ordering matters, not mere presence: transcribe appearing later in
    the workflow doesn't help a burn_subtitles that runs before it."""
    proposal = Proposal(
        summary="caption then transcribe",
        workflow=[
            ProposalStage(stage="burn_subtitles"),
            ProposalStage(stage="transcribe"),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert any("position 0" in error for error in result.errors)


def test_video_is_available_from_the_start_without_a_producer() -> None:
    """Every workflow begins with an uploaded video, so an ordinary
    video-in/video-out stage must not need a predecessor to supply it."""
    proposal = Proposal(
        summary="just crop",
        workflow=[ProposalStage(stage="crop", params={"aspect_ratio": "9:16"})],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True


def test_asset_errors_are_collected_alongside_other_errors() -> None:
    """Asset problems join the same ValidationResult as param/stage ones, so
    one retry round-trip can carry every fix rather than uncovering them
    one at a time."""
    proposal = Proposal(
        summary="...",
        workflow=[
            ProposalStage(stage="burn_subtitles"),
            ProposalStage(stage="color", params={"brightness": "not a float"}),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert len(result.errors) >= 2
    assert any("srt" in error for error in result.errors)
    assert any("must be float" in error for error in result.errors)


def test_unknown_stage_does_not_poison_the_asset_scan() -> None:
    """An unregistered stage is reported once and skipped; it must not also
    produce a confusing cascade of asset errors for everything after it."""
    proposal = Proposal(
        summary="...",
        workflow=[
            ProposalStage(stage="nonexistent"),
            ProposalStage(stage="crop", params={"aspect_ratio": "9:16"}),
        ],
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert "unknown stage 'nonexistent'" in result.errors
    assert not any("needs" in error for error in result.errors)


# --- numeric type tolerance (Phase 5C) -------------------------------------


def test_int_is_accepted_where_a_float_param_is_declared() -> None:
    """JSON has one number type, so a planner writes `1` as readily as
    `1.0`. Rejecting the former would send it round the regenerate loop
    over a value that was never wrong."""
    proposal = Proposal(
        summary="...", workflow=[ProposalStage(stage="color", params={"brightness": 1})]
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is True


def test_bool_is_not_accepted_as_a_number() -> None:
    """Python's bool subclasses int, so a naive isinstance check would let
    `"brightness": true` through as a valid number."""
    proposal = Proposal(
        summary="...", workflow=[ProposalStage(stage="color", params={"brightness": True})]
    )

    result = validate_proposal(proposal, _REGISTRY)

    assert result.valid is False
    assert any("must be float" in error for error in result.errors)


def test_string_is_still_rejected_for_a_float_param() -> None:
    proposal = Proposal(
        summary="...", workflow=[ProposalStage(stage="color", params={"brightness": "lots"})]
    )

    assert validate_proposal(proposal, _REGISTRY).valid is False


def test_first_stage_must_name_the_video_it_applies_to() -> None:
    """Observed live in Phase 6: the planner proposed [crop, render] with
    empty video_ids. Every other check passed, then stage 0 dead-lettered
    with "no input video" — the exact failure this validator exists to
    turn into something the planner can fix instead."""
    proposal = Proposal(
        summary="crop it",
        workflow=[ProposalStage(stage="crop", video_ids=[], params={"aspect_ratio": "1:1"})],
    )

    result = validate_proposal(
        proposal, _REGISTRY, known_video_handles=frozenset({"video_1"})
    )

    assert result.valid is False
    assert any("video_ids" in error for error in result.errors)


def test_later_stages_need_no_video_ids() -> None:
    """They take their input from the stage before them, so naming a video
    is meaningless there — only the first stage has nothing upstream."""
    proposal = Proposal(
        summary="crop then grade",
        workflow=[
            ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "1:1"}),
            ProposalStage(stage="color", video_ids=[], params={"brightness": 0.2}),
        ],
    )

    result = validate_proposal(
        proposal, _REGISTRY, known_video_handles=frozenset({"video_1"})
    )

    assert result.valid is True, result.errors


def test_a_must_be_first_stage_capability_is_rejected_out_of_position() -> None:
    """Observed live: the planner proposed [trim, merge] -- merge as stage
    1 rather than 0. Every other check passed, the room approved it, and
    it dead-lettered at execution (merge_worker.py refuses to run anywhere
    but stage 0, since later stages only receive the video the stage
    before them produced, not the project's real uploads merge needs).
    This is the check that turns that into a fixable ValidationResult
    instead of a wasted vote and a doomed job."""
    registry = CapabilityRegistry(
        {
            "trim": StageCapability(
                name="trim", description="trim", parameter_schema={"start": float, "end": float}
            ),
            "merge": StageCapability(
                name="merge", description="merge", must_be_first_stage=True
            ),
        }
    )
    proposal = Proposal(
        summary="trim then merge",
        workflow=[
            ProposalStage(stage="trim", video_ids=["video_1"], params={"start": 0.0, "end": 10.0}),
            ProposalStage(stage="merge", video_ids=["video_1", "video_2"], params={}),
        ],
    )

    result = validate_proposal(
        proposal, registry, known_video_handles=frozenset({"video_1", "video_2"})
    )

    assert result.valid is False
    assert any("merge" in error and "position 1" in error for error in result.errors)


def test_a_must_be_first_stage_capability_at_position_zero_is_accepted() -> None:
    registry = CapabilityRegistry(
        {
            "merge": StageCapability(
                name="merge", description="merge", must_be_first_stage=True
            ),
            "trim": StageCapability(
                name="trim", description="trim", parameter_schema={"start": float, "end": float}
            ),
        }
    )
    proposal = Proposal(
        summary="merge then trim",
        workflow=[
            ProposalStage(stage="merge", video_ids=["video_1", "video_2"], params={}),
            ProposalStage(stage="trim", video_ids=[], params={"start": 0.0, "end": 10.0}),
        ],
    )

    result = validate_proposal(
        proposal, registry, known_video_handles=frozenset({"video_1", "video_2"})
    )

    assert result.valid is True, result.errors


def test_a_first_stage_needing_no_video_is_exempt() -> None:
    """dummy declares it consumes nothing, so requiring it to name a video
    would be wrong."""
    registry = CapabilityRegistry(
        {
            "dummy": StageCapability(
                name="dummy",
                description="no-op",
                requires_asset_kinds=(),
                produces_asset_kinds=(),
            )
        }
    )
    proposal = Proposal(summary="...", workflow=[ProposalStage(stage="dummy", video_ids=[])])

    assert validate_proposal(
        proposal, registry, known_video_handles=frozenset({"video_1"})
    ).valid is True
