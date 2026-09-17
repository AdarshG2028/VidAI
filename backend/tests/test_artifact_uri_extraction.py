"""Result.artifact_uri population (Phase 5A, Step 6).

The one deliberate Setu-core change in Phase 5: StageProcessingService
lifts a stage's output video URI out of the returned payload into
Result's dedicated column. Purely additive — every worker that returns no
assets keeps getting NULL, exactly as before this existed.

Tested as a pure function here; the persisted-column behaviour is covered
by the worker/engine integration tests, which need Postgres.
"""

from backend.services.stage_processing_service import _primary_video_uri
from backend.workers.media import Asset, AssetKind, assets_payload


def test_extracts_the_video_uri_from_an_asset_payload() -> None:
    payload = assets_payload([Asset(kind=AssetKind.VIDEO, uri="local://out.mp4")])

    assert _primary_video_uri(payload) == "local://out.mp4"


def test_picks_the_video_out_of_a_mixed_asset_list() -> None:
    """transcribe returns a transcript and an srt alongside the video it
    passed through — the column tracks the video, not whatever came first."""
    payload = assets_payload(
        [
            Asset(kind=AssetKind.TRANSCRIPT, uri="local://t.json"),
            Asset(kind=AssetKind.SRT, uri="local://c.srt"),
            Asset(kind=AssetKind.VIDEO, uri="local://out.mp4"),
        ]
    )

    assert _primary_video_uri(payload) == "local://out.mp4"


def test_returns_none_for_workers_that_produce_no_assets() -> None:
    """DummyWorker and VideoAnalysisWorker predate this convention and must
    keep working untouched — this is what makes the change additive."""
    assert _primary_video_uri({"processed_by": "dummy"}) is None
    assert _primary_video_uri({"duration_seconds": 3.0, "width": 1280}) is None


def test_returns_none_when_assets_contain_no_video() -> None:
    payload = assets_payload([Asset(kind=AssetKind.SRT, uri="local://c.srt")])

    assert _primary_video_uri(payload) is None


def test_tolerates_a_malformed_payload_without_raising() -> None:
    """This runs inside the commit path of every successful stage. A
    surprising payload shape must not turn a completed stage into a
    failed one, so it degrades to NULL rather than raising."""
    assert _primary_video_uri({"assets": "not a list"}) is None
    assert _primary_video_uri({"assets": ["not a dict"]}) is None
    assert _primary_video_uri({"assets": [{"kind": "video"}]}) is None
    assert _primary_video_uri({"assets": [{"kind": "video", "uri": 42}]}) is None
    assert _primary_video_uri({}) is None
