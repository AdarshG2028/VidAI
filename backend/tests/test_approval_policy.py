"""Approval policy arithmetic (Phase 9a, step 1).

This is the phase's actual logic — everything else is plumbing — and it
needs no database, no app and no LLM, so it is tested exhaustively here
rather than through endpoints.
"""

import uuid

import pytest

from backend.services.approval_policy import (
    DEFAULT_POLICY,
    ApprovalPolicy,
    Decision,
    is_rejected,
    is_satisfied,
)

ADMIN = uuid.uuid4()
ALICE = uuid.uuid4()
BOB = uuid.uuid4()

APPROVE, REJECT = Decision.APPROVE, Decision.REJECT


def satisfied(policy, member_count, approvals) -> bool:
    return is_satisfied(policy, member_count, approvals, admin_user_id=ADMIN)


def rejected(policy, member_count, approvals) -> bool:
    return is_rejected(policy, member_count, approvals, admin_user_id=ADMIN)


def test_team_is_the_default() -> None:
    """The framing this project chose: small teams decide collectively.
    Safe as a default only because previews are exempt from policy."""
    assert DEFAULT_POLICY == ApprovalPolicy.TEAM


# --- admin -----------------------------------------------------------------


def test_admin_alone_decides() -> None:
    assert satisfied(ApprovalPolicy.ADMIN, 3, {ADMIN: APPROVE})
    assert rejected(ApprovalPolicy.ADMIN, 3, {ADMIN: REJECT})


def test_other_members_votes_do_not_carry_an_admin_proposal() -> None:
    """Advisory, not decisive. A room could unanimously approve and still
    be waiting on the one person whose sign-off the policy names."""
    everyone_else = {ALICE: APPROVE, BOB: APPROVE}

    assert not satisfied(ApprovalPolicy.ADMIN, 3, everyone_else)
    assert not rejected(ApprovalPolicy.ADMIN, 3, everyone_else)


def test_other_members_cannot_reject_an_admin_proposal() -> None:
    assert not rejected(ApprovalPolicy.ADMIN, 3, {ALICE: REJECT, BOB: REJECT})


def test_an_admin_proposal_with_no_admin_vote_is_simply_open() -> None:
    """Neither satisfied nor rejected — the state most proposals are in."""
    assert not satisfied(ApprovalPolicy.ADMIN, 3, {})
    assert not rejected(ApprovalPolicy.ADMIN, 3, {})


# --- team ------------------------------------------------------------------


def test_team_needs_everyone() -> None:
    assert not satisfied(ApprovalPolicy.TEAM, 3, {ADMIN: APPROVE, ALICE: APPROVE})
    assert satisfied(ApprovalPolicy.TEAM, 3, {ADMIN: APPROVE, ALICE: APPROVE, BOB: APPROVE})


def test_one_reject_ends_a_team_proposal_immediately() -> None:
    """Unanimity is already impossible, so the room is handed back its
    conversation rather than made to finish voting on a dead proposal."""
    assert rejected(ApprovalPolicy.TEAM, 3, {BOB: REJECT})
    assert not satisfied(ApprovalPolicy.TEAM, 3, {ADMIN: APPROVE, ALICE: APPROVE, BOB: REJECT})


def test_a_solo_room_approves_with_one_vote() -> None:
    """The single-user experience: you are the whole team."""
    assert satisfied(ApprovalPolicy.TEAM, 1, {ADMIN: APPROVE})


def test_a_team_proposal_nobody_has_voted_on_is_open() -> None:
    assert not satisfied(ApprovalPolicy.TEAM, 2, {})
    assert not rejected(ApprovalPolicy.TEAM, 2, {})


def test_a_member_leaving_mid_vote_does_not_deadlock_the_room() -> None:
    """member_count is recomputed from current membership, so the two
    remaining approvals now constitute unanimity. Counting `>=` rather
    than `==` is what stops a departed member's approval — filtered out by
    the caller — from making the total unreachable."""
    assert satisfied(ApprovalPolicy.TEAM, 2, {ADMIN: APPROVE, ALICE: APPROVE})


def test_an_empty_room_never_satisfies_a_team_proposal() -> None:
    """Unreachable through the API — creating a project makes its owner a
    member — but 'everyone approved' over nobody would spend real compute
    on zero votes."""
    assert not satisfied(ApprovalPolicy.TEAM, 0, {})


@pytest.mark.parametrize("policy", list(ApprovalPolicy))
def test_no_policy_is_ever_both_satisfied_and_rejected(policy: ApprovalPolicy) -> None:
    """The two transitions are mutually exclusive by construction, which
    is what lets the endpoint evaluate them independently without needing
    to decide which one wins."""
    for approvals in (
        {},
        {ADMIN: APPROVE},
        {ADMIN: REJECT},
        {ADMIN: APPROVE, ALICE: REJECT},
        {ADMIN: REJECT, ALICE: APPROVE},
        {ADMIN: APPROVE, ALICE: APPROVE, BOB: APPROVE},
        {ADMIN: REJECT, ALICE: REJECT, BOB: REJECT},
    ):
        for count in (1, 2, 3):
            both = satisfied(policy, count, approvals) and rejected(policy, count, approvals)
            assert not both, f"{policy} n={count} {approvals}"
