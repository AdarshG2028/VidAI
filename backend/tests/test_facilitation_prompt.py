"""The planner as a facilitator (Phase 9a, step 3).

Facilitation is a *prompt* capability, not code — so what is testable here
is that the prompt actually carries what facilitation needs: who said
what, that more than one person is present, and what the room's rule is.
Whether a given model then behaves well is prompt iteration, not a
property this suite can assert.
"""

import datetime as dt
import uuid

import pytest

from backend.models import Message, MessageRole, Project
from backend.services.capability_registry import DEFAULT_CAPABILITY_REGISTRY
from backend.services.planner_context import PlannerContext, participant_handles
from backend.services.prompt_builder import PromptBuilder

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def _msg(sender, content: str, minute: int = 0, role=MessageRole.USER) -> Message:
    return Message(
        conversation_id=uuid.uuid4(),
        sender_id=sender,
        role=role,
        content=content,
        created_at=dt.datetime(2026, 8, 1, 10, minute, tzinfo=dt.UTC),
    )


def _context(history: list[Message], *, policy: str | None = None) -> PlannerContext:
    return PlannerContext(
        project=Project(id=uuid.uuid4(), owner_id=ALICE, name="room"),
        conversation_history=history,
        videos=[],
        preferences=None,
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        approval_policy=policy,
    )


# --- attribution ------------------------------------------------------------


def test_each_speaker_gets_a_stable_handle() -> None:
    """No users table means no names, and a raw UUID is both unreadable and
    something models echo unreliably — the same reasoning that produced
    video_1 handles."""
    history = [_msg(ALICE, "one"), _msg(BOB, "two"), _msg(ALICE, "three")]

    handles = participant_handles(history)

    assert handles == {ALICE: "member_1", BOB: "member_2"}


def test_the_assistant_is_not_a_participant() -> None:
    history = [_msg(ALICE, "one"), _msg(None, "{}", role=MessageRole.ASSISTANT)]

    assert participant_handles(history) == {ALICE: "member_1"}


def test_user_turns_reach_the_model_attributed_and_timed() -> None:
    """A planner that cannot tell who said what cannot notice that two
    members want opposite things — the single most important thing it has
    to notice in a shared room."""
    history = [_msg(ALICE, "Crop vertically.", 41), _msg(BOB, "Keep landscape.", 42)]

    prompt = PromptBuilder().build(_context(history))

    assert prompt.messages[0]["content"] == "member_1 (10:41): Crop vertically."
    assert prompt.messages[1]["content"] == "member_2 (10:42): Keep landscape."


def test_the_planners_own_turns_are_not_labelled() -> None:
    """Prefixing them would teach the model to emit the prefix back inside
    its JSON."""
    history = [_msg(ALICE, "hi"), _msg(None, '{"type": "message"}', role=MessageRole.ASSISTANT)]

    prompt = PromptBuilder().build(_context(history))

    assert prompt.messages[1]["content"] == '{"type": "message"}'


# --- facilitation instructions ---------------------------------------------


def test_a_solo_room_gets_no_facilitation_section() -> None:
    """A single user does not need to be told to watch for disagreement,
    and asking for a summary of "the discussion" invites a summary of a
    monologue."""
    prompt = PromptBuilder().build(_context([_msg(ALICE, "hi")]))

    assert "facilitating" not in prompt.system


def test_a_shared_room_is_told_to_facilitate_not_to_obey() -> None:
    history = [_msg(ALICE, "Crop vertically."), _msg(BOB, "Keep landscape.")]

    system = PromptBuilder().build(_context(history)).system

    assert "facilitating" in system
    assert "whoever spoke last" in system
    assert "reasoning" in system and "discussion_summary" in system


def test_the_planner_is_told_not_to_pick_a_side_silently() -> None:
    """The Alice/Bob case from the roadmap: the correct response to a
    conflict is a clarifying message, not a proposal that quietly chooses."""
    history = [_msg(ALICE, "Crop vertically."), _msg(BOB, "Keep landscape.")]

    system = PromptBuilder().build(_context(history)).system

    assert "Do NOT propose a workflow that silently picks a side" in system
    assert "Never invent agreement" in system


# --- policy-aware wording ---------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("team", "approval from all team members"),
        ("admin", "ready for the owner's approval"),
    ],
)
def test_the_prompt_describes_the_rooms_actual_rule(policy: str, expected: str) -> None:
    """Wording only — the planner never evaluates the policy. But a message
    saying "I'll start rendering" when the room still has to vote would be
    a lie the model had no way of avoiding."""
    history = [_msg(ALICE, "one"), _msg(BOB, "two")]

    system = PromptBuilder().build(_context(history, policy=policy)).system

    assert expected in system
    assert "never that it is running" in system


def test_team_wording_rules_out_a_majority() -> None:
    """`team` is unanimity. A planner that told the room a majority would
    do would be describing a policy this system does not implement."""
    history = [_msg(ALICE, "one"), _msg(BOB, "two")]

    system = PromptBuilder().build(_context(history, policy="team")).system

    assert "EVERY active member" in system
    assert "never that a majority is enough" in system


def test_previews_are_described_as_exempt() -> None:
    history = [_msg(ALICE, "one"), _msg(BOB, "two")]

    system = PromptBuilder().build(_context(history, policy="team")).system

    assert "Previews are exempt" in system


def test_a_solo_room_states_the_policy_without_the_voting_lecture() -> None:
    """The rule still applies — one member is trivially unanimous — but
    telling a lone user to wait for "all team members" would be absurd."""
    system = PromptBuilder().build(_context([_msg(ALICE, "hi")], policy="team")).system

    assert "Approval policy for this room: team" in system
    assert "all team members" not in system
