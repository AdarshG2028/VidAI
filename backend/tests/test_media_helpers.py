"""Pure-logic tests for backend/workers/media.py.

No ffmpeg, no storage, no database — everything here is a function of its
arguments, which is the point of keeping these helpers separate from the
subprocess/storage half.
"""

import uuid

import pytest

from backend.workers.base import PermanentError, StageMessage
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    MediaProcessingError,
    _orientation,
    _parse_duration,
    _parse_frame_rate,
    assets_payload,
    extract_video_metadata,
    forward_assets,
    previous_assets,
    primary_video,
    resolve_input_uri,
    stage_params,
    stage_video_uris,
)

ORIGINAL = "local://original.mp4"
CROPPED = "local://cropped.mp4"


def _message(
    stage: int = 0,
    workflow: list[str] | None = None,
    stage_params_payload: dict | None = None,
) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=workflow if workflow is not None else ["crop"],
        payload={"stage_params": stage_params_payload}
        if stage_params_payload is not None
        else {},
    )


# --- Asset serialization ---------------------------------------------------


def test_asset_round_trips_through_dict() -> None:
    asset = Asset(kind=AssetKind.SRT, uri="local://captions.srt")

    assert Asset.from_dict(asset.to_dict()) == asset


def test_asset_from_dict_raises_on_malformed_entry() -> None:
    # Loud failure is deliberate: this only ever parses our own
    # serialization, so a missing field is a bug, not data to skip.
    with pytest.raises(KeyError):
        Asset.from_dict({"kind": "video"})


# --- Reading this stage's compiled slice -----------------------------------


def test_stage_params_reads_this_stages_entry_not_another() -> None:
    message = _message(
        stage=1,
        workflow=["crop", "color"],
        stage_params_payload={
            "0": {"params": {"aspect_ratio": "9:16"}, "video_uris": [ORIGINAL]},
            "1": {"params": {"brightness": 0.3}, "video_uris": [ORIGINAL]},
        },
    )

    assert stage_params(message) == {"brightness": 0.3}
    assert stage_video_uris(message) == [ORIGINAL]


def test_stage_accessors_tolerate_a_payload_with_no_stage_params() -> None:
    # dummy and video_analysis jobs carry payloads this key never appears in.
    message = _message()

    assert stage_params(message) == {}
    assert stage_video_uris(message) == []


# --- previous_assets / primary_video ---------------------------------------


def test_previous_assets_is_empty_for_the_first_stage() -> None:
    assert previous_assets(None) == []


def test_previous_assets_is_empty_for_workers_predating_the_convention() -> None:
    # DummyWorker returns {"processed_by": ...} with no "assets" key.
    assert previous_assets({"processed_by": "dummy"}) == []


def test_previous_assets_parses_the_asset_list() -> None:
    payload = {
        "assets": [
            {"kind": "video", "uri": CROPPED},
            {"kind": "srt", "uri": "local://captions.srt"},
        ]
    }

    assert previous_assets(payload) == [
        Asset(kind=AssetKind.VIDEO, uri=CROPPED),
        Asset(kind=AssetKind.SRT, uri="local://captions.srt"),
    ]


def test_primary_video_selects_the_video_regardless_of_position() -> None:
    assets = [
        Asset(kind=AssetKind.SRT, uri="local://captions.srt"),
        Asset(kind=AssetKind.VIDEO, uri=CROPPED),
    ]

    assert primary_video(assets) == Asset(kind=AssetKind.VIDEO, uri=CROPPED)


def test_primary_video_is_none_when_no_video_is_present() -> None:
    assert primary_video([Asset(kind=AssetKind.TRANSCRIPT, uri="local://t.json")]) is None


# --- forward_assets: monotonic accumulation --------------------------------


def test_forward_assets_replaces_the_video_and_keeps_everything_else() -> None:
    """The core chaining property: color reproduces only the video, and an
    .srt produced earlier must survive to reach burn_subtitles later."""
    previous = [
        Asset(kind=AssetKind.VIDEO, uri=ORIGINAL),
        Asset(kind=AssetKind.SRT, uri="local://captions.srt"),
        Asset(kind=AssetKind.TRANSCRIPT, uri="local://t.json"),
    ]
    produced = [Asset(kind=AssetKind.VIDEO, uri=CROPPED)]

    result = forward_assets(previous, produced)

    assert primary_video(result) == Asset(kind=AssetKind.VIDEO, uri=CROPPED)
    assert Asset(kind=AssetKind.SRT, uri="local://captions.srt") in result
    assert Asset(kind=AssetKind.TRANSCRIPT, uri="local://t.json") in result
    assert len(result) == 3


def test_forward_assets_adds_newly_produced_kinds() -> None:
    """transcribe passes the video through untouched and adds two kinds."""
    previous = [Asset(kind=AssetKind.VIDEO, uri=ORIGINAL)]
    produced = [
        Asset(kind=AssetKind.TRANSCRIPT, uri="local://t.json"),
        Asset(kind=AssetKind.SRT, uri="local://captions.srt"),
    ]

    result = forward_assets(previous, produced)

    assert len(result) == 3
    assert primary_video(result) == Asset(kind=AssetKind.VIDEO, uri=ORIGINAL)


def test_forward_assets_from_an_empty_previous_list() -> None:
    produced = [Asset(kind=AssetKind.VIDEO, uri=CROPPED)]

    assert forward_assets([], produced) == produced


def test_forward_assets_keeps_every_asset_when_a_kind_repeats() -> None:
    """Substituting in place would drop all but one of these; replacing the
    whole kind-group does not."""
    previous = [Asset(kind=AssetKind.SRT, uri="local://old.srt")]
    produced = [
        Asset(kind=AssetKind.SRT, uri="local://en.srt"),
        Asset(kind=AssetKind.SRT, uri="local://fr.srt"),
    ]

    result = forward_assets(previous, produced)

    assert result == produced
    assert Asset(kind=AssetKind.SRT, uri="local://old.srt") not in result


def test_forward_assets_survives_a_full_chain() -> None:
    """crop -> transcribe -> color -> burn_subtitles: the srt produced at
    step 2 must still be reachable at step 4, with color in between."""
    assets = forward_assets([], [Asset(kind=AssetKind.VIDEO, uri=CROPPED)])
    assets = forward_assets(
        assets,
        [
            Asset(kind=AssetKind.VIDEO, uri=CROPPED),
            Asset(kind=AssetKind.SRT, uri="local://captions.srt"),
        ],
    )
    assets = forward_assets(assets, [Asset(kind=AssetKind.VIDEO, uri="local://color.mp4")])

    assert primary_video(assets) == Asset(kind=AssetKind.VIDEO, uri="local://color.mp4")
    assert Asset(kind=AssetKind.SRT, uri="local://captions.srt") in assets


# --- assets_payload --------------------------------------------------------


def test_assets_payload_produces_the_persisted_shape() -> None:
    assets = [Asset(kind=AssetKind.VIDEO, uri=CROPPED)]

    assert assets_payload(assets) == {"assets": [{"kind": "video", "uri": CROPPED}]}


def test_assets_payload_round_trips_back_through_previous_assets() -> None:
    assets = [
        Asset(kind=AssetKind.VIDEO, uri=CROPPED),
        Asset(kind=AssetKind.SRT, uri="local://captions.srt"),
    ]

    assert previous_assets(assets_payload(assets)) == assets


# --- resolve_input_uri -----------------------------------------------------


def test_resolve_input_uri_falls_back_to_the_upload_at_the_first_stage() -> None:
    message = _message(
        stage_params_payload={"0": {"params": {}, "video_uris": [ORIGINAL]}}
    )

    assert resolve_input_uri(message, None) == ORIGINAL


def test_resolve_input_uri_prefers_the_previous_stages_output() -> None:
    """Without this the chain is broken: every stage would re-read the
    original upload and silently discard the prior stage's work."""
    message = _message(
        stage=1,
        workflow=["crop", "color"],
        stage_params_payload={"1": {"params": {}, "video_uris": [ORIGINAL]}},
    )
    previous_output = {"assets": [{"kind": "video", "uri": CROPPED}]}

    assert resolve_input_uri(message, previous_output) == CROPPED


def test_resolve_input_uri_falls_back_when_upstream_produced_no_video() -> None:
    # A prior stage that emitted only a transcript leaves the video to be
    # taken from the compiled uris.
    message = _message(
        stage=1,
        workflow=["transcribe", "color"],
        stage_params_payload={"1": {"params": {}, "video_uris": [ORIGINAL]}},
    )
    previous_output = {"assets": [{"kind": "transcript", "uri": "local://t.json"}]}

    assert resolve_input_uri(message, previous_output) == ORIGINAL


def test_resolve_input_uri_raises_permanently_when_there_is_no_input_at_all() -> None:
    message = _message(stage_params_payload={"0": {"params": {}, "video_uris": []}})

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        resolve_input_uri(message, None)

    # Must be permanent: retrying cannot conjure an input video, so the
    # harness should DLQ it rather than spend the retry budget.
    assert isinstance(exc_info.value, PermanentError)
    assert isinstance(exc_info.value, MediaProcessingError)


def test_resolve_input_uri_error_names_the_stage_without_crashing() -> None:
    # The message is built on a failure path; a malformed workflow must not
    # turn a clean InvalidMediaParamsError into an IndexError.
    message = _message(stage=7, workflow=["crop"], stage_params_payload={})

    with pytest.raises(InvalidMediaParamsError, match="stage 7"):
        resolve_input_uri(message, None)


# --- extract_video_metadata / its parsing helpers --------------------------
# Moved from tests/test_video_analysis_worker.py alongside the code they
# cover, when it moved from video_analysis_worker.py into this module —
# shared now by upload-time analysis and video_chain.measure() alike.

_FAKE_LANDSCAPE_PROBE = {
    "format": {"duration": "12.345"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "24000/1001",
        },
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


def test_extract_video_metadata_from_landscape_probe() -> None:
    metadata = extract_video_metadata(_FAKE_LANDSCAPE_PROBE)

    assert metadata["width"] == 1920
    assert metadata["height"] == 1080
    assert metadata["resolution"] == "1920x1080"
    assert metadata["orientation"] == "landscape"
    assert metadata["codec"] == "h264"
    assert metadata["duration_seconds"] == 12.345
    assert metadata["fps"] == pytest.approx(23.976, abs=0.001)


def test_extract_video_metadata_raises_when_no_video_stream() -> None:
    # video_stream()'s own check, reused rather than duplicated -- see
    # test_media_ffmpeg.py / worker tests for what no-video-stream input
    # translates to at each worker's own boundary (e.g.
    # video_analysis_worker.UnsupportedVideoError).
    with pytest.raises(InvalidMediaParamsError):
        extract_video_metadata({"format": {}, "streams": [{"codec_type": "audio"}]})


def test_orientation_portrait_when_taller_than_wide() -> None:
    assert _orientation(1080, 1920) == "portrait"


def test_orientation_landscape_when_wider_than_tall() -> None:
    assert _orientation(1920, 1080) == "landscape"


def test_orientation_none_when_dimensions_missing() -> None:
    assert _orientation(None, 1080) is None
    assert _orientation(1920, None) is None


def test_parse_frame_rate_handles_ratio_string() -> None:
    assert _parse_frame_rate("30/1") == 30.0
    assert _parse_frame_rate("24000/1001") == pytest.approx(23.976, abs=0.001)


def test_parse_frame_rate_none_for_missing_or_malformed() -> None:
    assert _parse_frame_rate(None) is None
    assert _parse_frame_rate("") is None
    assert _parse_frame_rate("not-a-ratio") is None


def test_parse_duration_none_for_missing_or_malformed() -> None:
    assert _parse_duration({"format": {}}) is None
    assert _parse_duration({"format": {"duration": "not-a-number"}}) is None
    assert _parse_duration({"format": {"duration": "5.5"}}) == 5.5
