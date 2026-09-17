"""PlannerResponse (Changelog v8) -- the planner's structured output,
converted into a domain model immediately after the LLM call. Nothing
downstream of the planner passes the raw dict further (item 4: avoid raw
dictionaries throughout the application) -- ConversationService serializes
this back to the existing `{"type": ...}` wire shape only at the point it
writes Message.content.
"""

from dataclasses import dataclass
from typing import Any

from backend.services.proposal import Proposal

# JSON-schema for Groq's (and any future provider's) structured-output mode
# (item 6 -- schema-enforced, not "please return JSON"). Flat rather than a
# oneOf on `type`, since strict structured-output modes generally require
# every property to be present (nullable via a ["string", "null"] type)
# rather than a conditional shape. Sent with strict: False (see
# llm_client.py) since ProposalStage.params is deliberately an open dict --
# strict mode requires additionalProperties: false on every nested object,
# which can't represent per-stage params whose real shape is checked at
# runtime by validate_proposal, not by this schema.
PLANNER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["message", "proposal"]},
        "text": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
        # Phase 9a facilitation fields. Nullable because a clarifying
        # message carries neither, and strict structured-output modes
        # require every declared property to be present.
        "reasoning": {"type": ["string", "null"]},
        "discussion_summary": {"type": ["string", "null"]},
        "workflow": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string"},
                    "video_ids": {"type": "array", "items": {"type": "string"}},
                    "params": {"type": "object"},
                },
                "required": ["stage", "video_ids", "params"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "text", "summary", "workflow", "reasoning", "discussion_summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class PlannerResponse:
    type: str  # "message" | "proposal"
    message: str | None = None
    proposal: Proposal | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlannerResponse":
        response_type = data["type"]
        if response_type == "message":
            return cls(type="message", message=data["text"])
        if response_type == "proposal":
            return cls(
                type="proposal",
                proposal=Proposal.from_dict(
                    {
                        "summary": data.get("summary") or "",
                        "workflow": data.get("workflow") or [],
                        "reasoning": data.get("reasoning"),
                        "discussion_summary": data.get("discussion_summary"),
                    }
                ),
            )
        raise ValueError(f"unknown planner response type {response_type!r}")

    def to_dict(self) -> dict[str, Any]:
        """The wire shape stored in Message.content -- unchanged from what
        StaticPlanner has always produced, so no API consumer needs to
        change when a real planner replaces it."""
        if self.type == "message":
            return {"type": "message", "text": self.message}
        assert self.proposal is not None
        return {
            "type": "proposal",
            "summary": self.proposal.summary,
            "workflow": [
                {"stage": s.stage, "video_ids": s.video_ids, "params": s.params}
                for s in self.proposal.workflow
            ],
            "reasoning": self.proposal.reasoning,
            "discussion_summary": self.proposal.discussion_summary,
        }
