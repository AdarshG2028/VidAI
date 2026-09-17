"""How a room decides whether a proposal may run (Phase 9a).

**Two pure functions, no table.** A policy is an enum on the project plus
`is_satisfied` / `is_rejected`, so a future policy is a new branch here
rather than a schema change. Consensus *detection* -- who is arguing with
whom, whether the room has converged -- lives in the planner prompt.
Nothing in this module is aware that an LLM exists; it does policy
arithmetic on votes that have already been cast.

**Previews are not governed by any of this.** The roadmap raised the
worry that unanimity would grind once the preview loop existed --
nobody wants three sign-offs on an exploratory tweak -- and offered
"scope the policy per action" as a fix, noting it would add a dimension
to policy evaluation. It only does if previews go *through* approval.
They do not: a preview is cheap, reversible and produces a deliberately
low-resolution artifact, so it is simply not an approval-governed action,
and these functions keep the shape the roadmap gave them. Policy governs
exactly one transition: a pending proposal becoming a real submitted job.

**Votes are counted, never interpreted.** Callers pass `member_count` and
`approvals` covering only people who can actually vote --
`ProjectMemberRepository.count_active` / `active_user_ids`, which exclude
outstanding invitations. A vote from someone who has since left must not
hold a decision hostage, and an unaccepted invitation must not make
unanimity unreachable.
"""

import uuid
from collections.abc import Mapping
from enum import StrEnum

from backend.models.enums import Decision

__all__ = [
    "ApprovalPolicy",
    "Decision",
    "DEFAULT_POLICY",
    "is_rejected",
    "is_satisfied",
]


class ApprovalPolicy(StrEnum):
    # One designated approver decides; other members' votes are advisory
    # and do not count toward the outcome. Suits a lead-driven workflow.
    #
    # There is no approver column: the roadmap defers reviewer sets and
    # two-level approval to the backlog because they need data these two
    # policies do not. The project **owner** is therefore the approver --
    # the only designated person a room has. Deliberately kept verbally
    # distinct from *job ownership* (project_jobs.submitted_by_user_id,
    # which Phase 9b authorizes cancellation against): a room's approver
    # and a job's owner need not be the same person, and an early draft
    # conflated the two names.
    ADMIN = "admin"

    # Every active member must approve. Any single rejection ends it --
    # unanimity is already impossible, so waiting for the rest of the room
    # to vote would only delay a settled outcome.
    TEAM = "team"


# Chosen for the "small creative teams decide collectively" framing. Safe
# as a default now that previews are exempt -- the friction that made this
# questionable was per-tweak sign-off, which no longer happens.
DEFAULT_POLICY = ApprovalPolicy.TEAM


def is_satisfied(
    policy: ApprovalPolicy,
    member_count: int,
    approvals: Mapping[uuid.UUID, Decision],
    *,
    admin_user_id: uuid.UUID,
) -> bool:
    """Whether this proposal may now be compiled and submitted."""
    if policy == ApprovalPolicy.ADMIN:
        return approvals.get(admin_user_id) == Decision.APPROVE

    # Unanimity over a room that has members. `member_count == 0` cannot
    # happen through the API -- creating a project makes its owner a
    # member -- but it is guarded rather than left to vacuous truth,
    # because "everyone approved" over nobody would submit real compute on
    # zero votes.
    if member_count <= 0:
        return False

    # An explicit rejection is never overridden by a count. With callers
    # filtering votes to current members this is unreachable -- there
    # cannot be more votes than voters -- but the two functions are
    # evaluated independently by the approve endpoint, so if both could
    # ever be true at once the outcome would depend on which was checked
    # first, and one of those orders spends real compute on a proposal
    # somebody rejected. Cheap to make impossible instead of relying on
    # the caller's hygiene.
    if any(decision == Decision.REJECT for decision in approvals.values()):
        return False

    approved = sum(1 for decision in approvals.values() if decision == Decision.APPROVE)
    return approved >= member_count


def is_rejected(
    policy: ApprovalPolicy,
    member_count: int,
    approvals: Mapping[uuid.UUID, Decision],
    *,
    admin_user_id: uuid.UUID,
) -> bool:
    """Whether this proposal can no longer succeed and should end now.

    The mirror of `is_satisfied`, and not simply its negation: a `team`
    proposal with two of three votes in is neither satisfied nor rejected
    -- it is still open, which is the state most proposals spend most of
    their life in.
    """
    if policy == ApprovalPolicy.ADMIN:
        return approvals.get(admin_user_id) == Decision.REJECT

    # One reject makes unanimity unreachable, so the outcome is already
    # decided and the room should be handed back its conversation rather
    # than made to finish voting on a dead proposal.
    return any(decision == Decision.REJECT for decision in approvals.values())
