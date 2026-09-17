import pytest

from backend.services.capability_registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
    StageCapability,
)


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
