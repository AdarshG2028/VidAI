import pytest

from backend.services.proposal import Proposal, ProposalStage


def test_from_dict_parses_the_canonical_shape() -> None:
    data = {
        "summary": "Crop to 9:16 and normalize audio.",
        "workflow": [
            {"stage": "crop", "params": {"aspect_ratio": "9:16"}},
            {"stage": "audio", "params": {"normalize": True}},
        ],
    }

    proposal = Proposal.from_dict(data)

    assert proposal.summary == "Crop to 9:16 and normalize audio."
    assert proposal.workflow == [
        ProposalStage(stage="crop", params={"aspect_ratio": "9:16"}),
        ProposalStage(stage="audio", params={"normalize": True}),
    ]


def test_from_dict_defaults_missing_params_to_empty_dict() -> None:
    data = {"summary": "...", "workflow": [{"stage": "dummy"}]}

    proposal = Proposal.from_dict(data)

    assert proposal.workflow == [ProposalStage(stage="dummy", params={})]


def test_from_dict_defaults_missing_video_ids_to_empty_list() -> None:
    data = {"summary": "...", "workflow": [{"stage": "dummy"}]}

    proposal = Proposal.from_dict(data)

    assert proposal.workflow == [ProposalStage(stage="dummy", video_ids=[], params={})]


def test_from_dict_parses_video_ids() -> None:
    data = {
        "summary": "Combine two clips.",
        "workflow": [{"stage": "combine", "video_ids": ["video_1", "video_2"], "params": {}}],
    }

    proposal = Proposal.from_dict(data)

    assert proposal.workflow == [
        ProposalStage(stage="combine", video_ids=["video_1", "video_2"], params={})
    ]


def test_from_dict_raises_on_a_stage_missing_its_name() -> None:
    """Structurally malformed input (missing "stage") is a different
    failure mode from validate_proposal's semantic checks -- it's not
    a shape that could plausibly reference a registered capability."""
    data = {"summary": "...", "workflow": [{"params": {}}]}

    with pytest.raises(KeyError):
        Proposal.from_dict(data)
