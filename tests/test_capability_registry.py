import pytest

from backend.services.capability_registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
    StageCapability,
)
from backend.services.proposal import Proposal, ProposalStage
from backend.services.proposal_validator import validate_proposal


def test_register_then_get_returns_it() -> None:
    registry = CapabilityRegistry()
    capability = StageCapability(name="crop", description="crop a video")

    registry.register(capability)

    assert registry.get("crop") is capability


def test_get_returns_none_for_unregistered_stage() -> None:
    registry = CapabilityRegistry()

    assert registry.get("nonexistent") is None


def test_exists_reflects_registration_state() -> None:
    registry = CapabilityRegistry()
    assert registry.exists("crop") is False

    registry.register(StageCapability(name="crop", description="crop a video"))

    assert registry.exists("crop") is True


def test_list_returns_every_registered_capability() -> None:
    registry = CapabilityRegistry()
    registry.register(StageCapability(name="crop", description="crop"))
    registry.register(StageCapability(name="color", description="color"))

    names = {capability.name for capability in registry.list()}

    assert names == {"crop", "color"}


def test_constructor_accepts_a_seed_dict() -> None:
    seed = {"dummy": StageCapability(name="dummy", description="no-op")}

    registry = CapabilityRegistry(seed)

    assert registry.exists("dummy") is True


def test_default_registry_has_the_dummy_stand_in_stage() -> None:
    assert DEFAULT_CAPABILITY_REGISTRY.exists("dummy") is True
    assert DEFAULT_CAPABILITY_REGISTRY.get("dummy").parameter_schema == {}


def test_asset_kinds_default_to_video_in_video_out() -> None:
    """The common case needs no declaration: crop/color/audio/trim/render
    all take a video and return one. Only the transcript pair diverges."""
    capability = StageCapability(name="crop", description="crop a video")

    assert capability.requires_asset_kinds == ("video",)
    assert capability.produces_asset_kinds == ("video",)


def test_asset_kinds_can_declare_a_producer_consumer_pair() -> None:
    """The shape 5F relies on: transcribe emits an srt alongside the video
    it passes through, and burn_subtitles consumes that srt."""
    transcribe = StageCapability(
        name="transcribe",
        description="transcribe speech",
        produces_asset_kinds=("video", "transcript", "srt"),
    )
    burn = StageCapability(
        name="burn_subtitles",
        description="burn captions in",
        requires_asset_kinds=("video", "srt"),
    )

    assert "srt" in transcribe.produces_asset_kinds
    assert "srt" in burn.requires_asset_kinds


def test_must_be_first_stage_defaults_to_false() -> None:
    assert StageCapability(name="crop", description="crop").must_be_first_stage is False


def test_default_registrys_merge_requires_first_position() -> None:
    """Regression guard: merge_worker.py refuses to run anywhere but stage
    0 (it would otherwise silently concatenate the original uploads and
    discard prior edits). This flag is what lets validate_proposal catch
    that before the room ever votes, instead of the job dead-lettering
    after approval -- observed live for `[trim, merge]`."""
    assert DEFAULT_CAPABILITY_REGISTRY.get("merge").must_be_first_stage is True


def test_asset_kind_defaults_are_immutable_and_unshared() -> None:
    """Tuples rather than lists, so the default can be a plain value: two
    capabilities can't end up aliasing one mutable default list, and no
    caller can mutate a capability's declared kinds in place."""
    first = StageCapability(name="crop", description="crop")
    second = StageCapability(name="color", description="color")

    assert first.requires_asset_kinds is second.requires_asset_kinds
    assert isinstance(first.requires_asset_kinds, tuple)
    with pytest.raises(AttributeError):
        first.requires_asset_kinds.append("srt")  # type: ignore[attr-defined]


# --- content search: the real registry, not a fake one ---------------------
#
# These deliberately use DEFAULT_CAPABILITY_REGISTRY where
# test_proposal_validator.py uses a fake. The question there is whether the
# chaining *rule* works; the question here is whether the three capabilities
# as actually registered declare kinds that satisfy it -- a typo in
# produces_asset_kinds passes every test in that file and still dead-letters
# every real job.


def test_find_content_chains_into_both_consumers() -> None:
    for consumer in ("remove_matches", "keep_matches"):
        proposal = Proposal(
            summary="...",
            workflow=[
                ProposalStage(stage="find_content", params={"query": "the red shirt"}),
                ProposalStage(stage=consumer, params={}),
            ],
        )

        result = validate_proposal(proposal, DEFAULT_CAPABILITY_REGISTRY)

        assert result.valid is True, f"find_content -> {consumer}: {result.errors}"


def test_a_consumer_without_find_content_is_rejected_before_the_vote() -> None:
    """The planner cannot see requires_asset_kinds -- prompt_builder renders
    only name/description/parameters -- so it can and will propose a bare
    remove_matches. This is what catches that while the proposal is still a
    proposal, rather than after the room has approved it."""
    for consumer in ("remove_matches", "keep_matches"):
        proposal = Proposal(summary="...", workflow=[ProposalStage(stage=consumer, params={})])

        result = validate_proposal(proposal, DEFAULT_CAPABILITY_REGISTRY)

        assert result.valid is False
        assert any(
            consumer in error and "content_matches" in error for error in result.errors
        ), f"the error should name the missing kind, got {result.errors}"


def test_find_content_may_run_as_the_first_stage() -> None:
    """[find_content, remove_matches] is the common case, so find_content
    must not inherit merge's first-stage restriction in either direction."""
    assert DEFAULT_CAPABILITY_REGISTRY.get("find_content").must_be_first_stage is False


def test_the_consumers_take_no_parameters_of_their_own() -> None:
    """The polarity lives in the capability name, never in a parameter. A
    'mode' param would be silently omittable -- the validator only
    type-checks params that were actually supplied -- and an inverted cut
    produces a plausible video containing exactly the wrong footage."""
    for consumer in ("remove_matches", "keep_matches"):
        assert DEFAULT_CAPABILITY_REGISTRY.get(consumer).parameter_schema == {}
