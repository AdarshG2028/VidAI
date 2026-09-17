import pytest

from backend.services.planner_response import PlannerResponse
from backend.services.proposal import Proposal, ProposalStage


def test_from_dict_message_type() -> None:
    response = PlannerResponse.from_dict({"type": "message", "text": "who is this for?"})

    assert response.type == "message"
    assert response.message == "who is this for?"


def test_from_dict_proposal_type_parses_workflow_and_video_ids() -> None:
    data = {
        "type": "proposal",
        "summary": "Crop it.",
        "text": None,
        "workflow": [{"stage": "crop", "video_ids": ["video_1"], "params": {"aspect_ratio": "9:16"}}],
    }

    response = PlannerResponse.from_dict(data)

    assert response.type == "proposal"
    assert response.proposal == Proposal(
        summary="Crop it.",
        workflow=[ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "9:16"})],
    )


def test_from_dict_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        PlannerResponse.from_dict({"type": "plan"})


def test_to_dict_message_round_trips() -> None:
    response = PlannerResponse(type="message", message="hi")

    assert response.to_dict() == {"type": "message", "text": "hi"}


def test_to_dict_proposal_round_trips_video_ids() -> None:
    response = PlannerResponse(
        type="proposal",
        proposal=Proposal(
            summary="Combine.",
            workflow=[ProposalStage(stage="combine", video_ids=["video_1", "video_2"], params={})],
        ),
    )

    assert response.to_dict() == {
        "type": "proposal",
        "summary": "Combine.",
        "workflow": [{"stage": "combine", "video_ids": ["video_1", "video_2"], "params": {}}],
        # Phase 9a facilitation fields, null when the planner omits them.
        "reasoning": None,
        "discussion_summary": None,
    }
