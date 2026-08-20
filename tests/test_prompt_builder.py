import uuid

from backend.models import Message, MessageRole, Project
from backend.services.capability_registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
    StageCapability,
)
from backend.services.planner_context import PlannerContext, VideoContext
from backend.services.prompt_builder import PromptBuilder


def _context(*, history=None, videos=None, validation_feedback=None) -> PlannerContext:
    return PlannerContext(
        project=Project(id=uuid.uuid4(), owner_id=uuid.uuid4()),
        conversation_history=history or [],
        videos=videos or [],
        preferences=None,
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
    )


def test_system_prompt_lists_video_handles_and_capabilities() -> None:
    context = _context(
        videos=[VideoContext(handle="video_1", video_id=str(uuid.uuid4()), display_name="Intro")]
    )

    prompt = PromptBuilder().build(context)

    assert "video_1: Intro" in prompt.system
    assert "dummy" in prompt.system


def test_conversation_history_maps_to_chat_roles() -> None:
    history = [
        Message(conversation_id=uuid.uuid4(), role=MessageRole.USER, content="hi"),
        Message(conversation_id=uuid.uuid4(), role=MessageRole.ASSISTANT, content='{"type": "message", "text": "ok"}'),
    ]

    prompt = PromptBuilder().build(_context(history=history))

    assert prompt.messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": '{"type": "message", "text": "ok"}'},
    ]


def test_validation_feedback_appends_one_extra_user_turn() -> None:
    prompt = PromptBuilder().build(_context(), validation_feedback=["unknown stage 'crp'"])

    assert len(prompt.messages) == 1
    assert prompt.messages[0]["role"] == "user"
    assert "unknown stage 'crp'" in prompt.messages[0]["content"]


def test_no_validation_feedback_appends_nothing() -> None:
    prompt = PromptBuilder().build(_context())

    assert prompt.messages == []


# --- parameter schema rendering (Phase 5B) ---------------------------------


def _system_for(registry) -> str:
    context = PlannerContext(
        project=None,
        conversation_history=[],
        videos=[],
        preferences=None,
        capability_registry=registry,
        approval_policy=None,
    )
    return PromptBuilder().build(context).system


def test_capability_parameters_are_declared_not_left_to_inference() -> None:
    """The planner must read parameter names off the schema, not guess them
    from the description's prose. Observed live before this: asked to
    "rotate 90 degrees clockwise", it replied asking the *user* what the
    parameter was called, because the prompt never said `degrees`."""
    registry = CapabilityRegistry(
        {
            "rotate": StageCapability(
                name="rotate",
                description="Rotate by a quarter turn.",
                parameter_schema={"degrees": int},
            )
        }
    )

    system = _system_for(registry)

    assert "degrees (integer)" in system


def test_parameter_types_are_named_in_json_terms() -> None:
    """A planner emitting JSON reasons about 'string'/'number', not about
    Python's str/float."""
    registry = CapabilityRegistry(
        {
            "color": StageCapability(
                name="color",
                description="Adjust the picture.",
                parameter_schema={"brightness": float, "preset": str, "passes": int},
            )
        }
    )

    system = _system_for(registry)

    assert "brightness (number)" in system
    assert "preset (string)" in system
    assert "passes (integer)" in system


def test_capabilities_without_parameters_render_no_parameter_line() -> None:
    registry = CapabilityRegistry(
        {"dummy": StageCapability(name="dummy", description="No-op.", parameter_schema={})}
    )

    assert "parameters:" not in _system_for(registry)


# --- stored preferences (Phase 6) ------------------------------------------


def _system_with_preferences(preferences) -> str:
    context = PlannerContext(
        project=None,
        conversation_history=[],
        videos=[],
        preferences=preferences,
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        approval_policy=None,
    )
    return PromptBuilder().build(context).system


def test_preferences_render_as_readable_facts_not_an_object_repr() -> None:
    """This branch used to interpolate the ORM object, so the planner was
    handed `<UserPreference object at 0x...>` and ignored it. The bug
    survived because nothing wrote preferences until Phase 6 — every prior
    run had none, so the branch never produced anything meaningful."""
    from backend.models import UserPreference

    system = _system_with_preferences(
        UserPreference(
            user_id=uuid.uuid4(), preferred_platform="linkedin", preferred_export_format="mp4"
        )
    )

    assert "object at 0x" not in system
    assert "linkedin" in system
    assert "mp4" in system


def test_only_set_preferences_are_rendered() -> None:
    """A line saying a preference is unset is noise that invites the model
    to treat absence as a decision."""
    from backend.models import UserPreference

    system = _system_with_preferences(
        UserPreference(user_id=uuid.uuid4(), preferred_platform="tiktok")
    )

    assert "tiktok" in system
    assert "resolution" not in system.lower().split("what this user has told you")[-1]


def test_a_user_with_no_preferences_adds_nothing() -> None:
    from backend.models import UserPreference

    system = _system_with_preferences(UserPreference(user_id=uuid.uuid4()))

    assert "told you before" not in system


def test_no_preferences_object_adds_nothing() -> None:
    assert "told you before" not in _system_with_preferences(None)


# --- video facts from analysis (found by live testing) ---------------------


def _system_with_videos(videos) -> str:
    context = PlannerContext(
        project=None,
        conversation_history=[],
        videos=videos,
        preferences=None,
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        approval_policy=None,
    )
    return PromptBuilder().build(context).system


def test_measured_video_facts_reach_the_planner() -> None:
    """Found by using it: asked to "trim the last 10 seconds", the planner
    replied asking how long the video was — despite video_analysis having
    measured and stored exactly that on upload. It was only ever shown the
    handle and a display name."""
    system = _system_with_videos(
        [
            VideoContext(
                handle="video_1",
                video_id="x",
                display_name="my clip",
                duration_seconds=140.8,
                resolution="1280x720",
                orientation="landscape",
            )
        ]
    )

    assert "140.8s" in system
    assert "1280x720" in system
    assert "landscape" in system


def test_a_video_without_analysis_still_renders() -> None:
    """Analysis may still be running or may have failed. A planner that can
    still propose something beats one that refuses."""
    system = _system_with_videos(
        [VideoContext(handle="video_1", video_id="x", display_name="unanalysed")]
    )

    assert "- video_1: unanalysed" in system
    assert "(" not in system.split("- video_1: unanalysed")[1].split("\n")[0]
