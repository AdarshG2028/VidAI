import pytest

from backend.services.proposal import Proposal, ProposalStage
from backend.services.workflow_compiler import (
    ExecutionContext,
    UnknownVideoHandleError,
    compile_workflow,
)


def test_compiles_to_the_exact_expected_workflow_and_payload_shape() -> None:
    proposal = Proposal(
        summary="Crop to 9:16 and brighten.",
        workflow=[
            ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "9:16"}),
            ProposalStage(stage="color", video_ids=["video_1"], params={"brightness": 1.1}),
        ],
    )
    context = ExecutionContext(
        video_uris={"video_1": "local:///videos/abc.mp4"},
        video_db_ids={"video_1": "11111111-1111-1111-1111-111111111111"},
    )

    workflow, payload = compile_workflow(proposal, context)

    assert workflow == ["crop", "color"]
    assert payload == {
        "stage_params": {
            "0": {
                "params": {"aspect_ratio": "9:16"},
                "video_uris": ["local:///videos/abc.mp4"],
                "video_ids": ["11111111-1111-1111-1111-111111111111"],
            },
            "1": {
                "params": {"brightness": 1.1},
                "video_uris": ["local:///videos/abc.mp4"],
                "video_ids": ["11111111-1111-1111-1111-111111111111"],
            },
        },
    }


def test_single_stage_proposal_compiles() -> None:
    proposal = Proposal(summary="...", workflow=[ProposalStage(stage="dummy", params={})])
    context = ExecutionContext(video_uris={})

    workflow, payload = compile_workflow(proposal, context)

    assert workflow == ["dummy"]
    assert payload == {
        "stage_params": {"0": {"params": {}, "video_uris": [], "video_ids": []}}
    }


def test_stage_referencing_multiple_videos_resolves_all_of_them() -> None:
    proposal = Proposal(
        summary="Combine two clips.",
        workflow=[ProposalStage(stage="combine", video_ids=["video_1", "video_2"], params={})],
    )
    context = ExecutionContext(
        video_uris={
            "video_1": "local:///videos/a.mp4",
            "video_2": "local:///videos/b.mp4",
        }
    )

    workflow, payload = compile_workflow(proposal, context)

    assert workflow == ["combine"]
    assert payload["stage_params"]["0"]["video_uris"] == [
        "local:///videos/a.mp4",
        "local:///videos/b.mp4",
    ]


def test_unknown_video_handle_raises_instead_of_crashing_with_keyerror() -> None:
    """Regression test: a proposal referencing a handle not in the
    ExecutionContext (e.g. the project's videos changed between proposal and
    confirm) used to raise a bare KeyError here, surfacing as an unhandled
    500. Must raise UnknownVideoHandleError instead so the API layer can
    turn it into a clean 4xx."""
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="dummy", video_ids=["video_99"], params={})],
    )
    context = ExecutionContext(video_uris={"video_1": "local:///videos/a.mp4"})

    with pytest.raises(UnknownVideoHandleError) as exc_info:
        compile_workflow(proposal, context)

    assert exc_info.value.video_id == "video_99"


# --- video_db_ids (Phase 10 foundation) -------------------------------------


def test_video_db_ids_default_to_empty_string_when_not_supplied() -> None:
    """A caller that only ever cared about bytes (video_uris) must keep
    working unchanged -- video_db_ids defaults to {}, so every handle
    degrades to "" rather than raising."""
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", video_ids=["video_1"], params={})],
    )
    context = ExecutionContext(video_uris={"video_1": "local://a.mp4"})

    _, payload = compile_workflow(proposal, context)

    assert payload["stage_params"]["0"]["video_ids"] == [""]


def test_video_db_ids_are_positionally_aligned_with_video_uris() -> None:
    proposal = Proposal(
        summary="Combine two clips.",
        workflow=[ProposalStage(stage="combine", video_ids=["video_1", "video_2"], params={})],
    )
    context = ExecutionContext(
        video_uris={
            "video_1": "local:///videos/a.mp4",
            "video_2": "local:///videos/b.mp4",
        },
        video_db_ids={"video_1": "id-a", "video_2": "id-b"},
    )

    _, payload = compile_workflow(proposal, context)

    assert payload["stage_params"]["0"]["video_ids"] == ["id-a", "id-b"]


# --- preview mode (Phase 5A, Step 8) ---------------------------------------


def test_preview_mode_flags_the_payload() -> None:
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "9:16"})],
    )
    context = ExecutionContext(video_uris={"video_1": "local://a.mp4"}, preview=True)

    _, payload = compile_workflow(proposal, context)

    assert payload["_preview"] is True


def test_full_mode_omits_the_preview_flag_entirely() -> None:
    """Absent rather than False, so a full render's payload is byte-for-byte
    what it was before preview existed — which matters because the
    idempotency key is derived from the compiled output."""
    proposal = Proposal(
        summary="...",
        workflow=[ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "9:16"})],
    )
    context = ExecutionContext(video_uris={"video_1": "local://a.mp4"})

    _, payload = compile_workflow(proposal, context)

    assert "_preview" not in payload


def test_preview_changes_nothing_else_about_the_compiled_output() -> None:
    """A preview must be evidence about the real render, so the stages and
    their params have to be identical — only the flag may differ."""
    proposal = Proposal(
        summary="...",
        workflow=[
            ProposalStage(stage="crop", video_ids=["video_1"], params={"aspect_ratio": "9:16"}),
            ProposalStage(stage="color", video_ids=["video_1"], params={"brightness": 1.1}),
        ],
    )
    uris = {"video_1": "local://a.mp4"}

    full_workflow, full_payload = compile_workflow(proposal, ExecutionContext(video_uris=uris))
    prev_workflow, prev_payload = compile_workflow(
        proposal, ExecutionContext(video_uris=uris, preview=True)
    )

    assert prev_workflow == full_workflow
    assert prev_payload["stage_params"] == full_payload["stage_params"]
    assert set(prev_payload) - set(full_payload) == {"_preview"}


def test_compiled_preview_flag_is_the_one_media_reads() -> None:
    """Producer/consumer agreement, asserted rather than assumed: a typo in
    either would silently disable preview and render everything at full
    quality with no error to notice."""
    import uuid as _uuid

    from backend.workers.base import StageMessage
    from backend.workers.media import is_preview

    proposal = Proposal(summary="...", workflow=[ProposalStage(stage="crop", video_ids=[])])
    _, payload = compile_workflow(proposal, ExecutionContext(video_uris={}, preview=True))

    message = StageMessage(job_id=_uuid.uuid4(), stage=0, workflow=["crop"], payload=payload)

    assert is_preview(message) is True
