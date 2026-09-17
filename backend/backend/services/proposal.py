"""Domain models for a proposal (§6) -- the planner's structured output,
before it's validated and compiled into a Setu job.

Frozen dataclasses, matching StageMessage's existing convention
(backend/workers/base.py) -- Pydantic stays this repo's API-schema layer
(backend/api/schemas/), not its internal-domain layer.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProposalStage:
    stage: str
    # Which of the project's video(s) this stage acts on, referenced by the
    # short LLM-facing handle PlannerContext assigns (e.g. "video_1"), never
    # a raw Video.id -- models are unreliable at echoing UUIDs verbatim.
    # ConversationService resolves handles to real storage URIs into
    # ExecutionContext.video_uris when building the ExecutionContext
    # (Changelog v8). Empty for stages that don't need one yet (Phase 3's
    # "dummy" stand-in never populated this).
    video_ids: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Proposal:
    summary: str
    workflow: list[ProposalStage]
    # Phase 9a. Why this workflow, and what the room actually discussed.
    # Optional because they are new: StaticPlanner produces neither, and a
    # live model will sometimes omit them. A proposal without its
    # rationale is worth less, not invalid.
    reasoning: str | None = None
    discussion_summary: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        """Parses the raw `{"summary": ..., "workflow": [...]}` shape (§6)
        -- the external representation a proposal arrives in, whether
        hand-authored (Phase 3) or LLM-generated (Phase 4). Raises KeyError
        if a workflow item is missing "stage": that's a structurally
        malformed proposal, a different failure mode from the semantic
        checks validate_proposal performs against the capability registry.
        """
        workflow = [
            ProposalStage(
                stage=item["stage"],
                video_ids=item.get("video_ids", []),
                params=item.get("params", {}),
            )
            for item in data.get("workflow", [])
        ]
        return cls(
            summary=data.get("summary", ""),
            workflow=workflow,
            reasoning=data.get("reasoning"),
            discussion_summary=data.get("discussion_summary"),
        )
