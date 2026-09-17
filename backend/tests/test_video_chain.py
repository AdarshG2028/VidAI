"""video_chain.recent_edits / planner_context.with_edit_history.

Regression coverage for the bug this module fixes: a room trimmed a video,
approved it, then trimmed again -- and the second trim silently ran on the
untouched original because nothing tracked what the first trim produced.

Also covers the bounded 3-state window this grew into: latest edit
(default), the edit before that (`_previous`), and the untouched original
(`_original`) -- not an arbitrary version history further back than that.
"""

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest

from backend.models import Job, JobStatus, Project, ProjectJob, Result, Video
from backend.services.planner_context import (
    VideoContext,
    _edit_note,
    with_edit_history,
)
from backend.services.video_chain import (
    VideoAnalysis,
    analysis_assets,
    measure,
    recent_edits,
    summarize_analysis,
)
from backend.storage.local import LocalDiskStorage
from backend.workers.media import PREVIEW_FLAG

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.usefixtures("database_url")


async def _project(session) -> Project:
    project = Project(id=uuid.uuid4(), owner_id=uuid.uuid4(), name="p")
    session.add(project)
    await session.flush()
    return project


async def _video(session, project_id: uuid.UUID, *, uri: str = "local:///orig.mp4") -> Video:
    video = Video(
        id=uuid.uuid4(),
        project_id=project_id,
        storage_uri=uri,
        original_filename="orig.mp4",
        name=None,
    )
    session.add(video)
    await session.flush()
    return video


async def _completed_job(
    session,
    project_id: uuid.UUID,
    *,
    stage_zero_video_ids: list[str],
    final_assets: list[dict] | None,
    preview: bool = False,
    completed_at: dt.datetime,
    workflow: list[str] | None = None,
    stage_zero_video_uris: list[str] | None = None,
) -> Job:
    payload: dict = {
        "stage_params": {
            "0": {
                "params": {},
                # What the job was compiled to read. recent_edits compares
                # the produced video against this to tell a real edit from
                # an analysis-only pass-through.
                "video_uris": stage_zero_video_uris or [],
                "video_ids": stage_zero_video_ids,
            }
        }
    }
    if preview:
        payload[PREVIEW_FLAG] = True
    job = Job(
        id=uuid.uuid4(),
        status=JobStatus.COMPLETED,
        workflow={"workflow": workflow or ["trim"]},
        current_stage=1,
        payload=payload,
        max_attempts=3,
        completed_at=completed_at,
    )
    session.add(job)
    await session.flush()
    if final_assets is not None:
        session.add(Result(job_id=job.id, worker_name="trim", stage=0, payload={"assets": final_assets}))
    session.add(
        ProjectJob(job_id=job.id, project_id=project_id, submitted_by_user_id=uuid.uuid4())
    )
    await session.flush()
    return job


def _video_asset(uri: str) -> dict:
    return {"kind": "video", "uri": uri}


async def test_recent_edits_empty_when_never_edited(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id)

    assert await recent_edits(session, project.id, video.id) == []


async def test_recent_edits_finds_completed_single_video_output(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id)
    job = await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///trimmed.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
    )

    edits = await recent_edits(session, project.id, video.id)

    assert len(edits) == 1
    assert edits[0].uri == "local:///trimmed.mp4"
    assert edits[0].job_id == job.id
    assert edits[0].stages == ["trim"]


async def test_recent_edits_reports_the_producing_jobs_stage_names(session) -> None:
    """The stage names a later proposal must not repeat -- see _edit_note's
    docstring for the real-conversation bug this data feeds the fix for."""
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///cut.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["remove_segment", "trim"],
    )

    edits = await recent_edits(session, project.id, video.id)

    assert edits[0].stages == ["remove_segment", "trim"]


async def test_recent_edits_ignores_preview_jobs(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///preview.mp4")],
        preview=True,
        completed_at=dt.datetime.now(dt.UTC),
    )

    assert await recent_edits(session, project.id, video.id) == []


async def test_recent_edits_ignores_multi_video_jobs(session) -> None:
    """A merge-style job (two inputs) advances no single video's chain --
    guessing which of the two it was would be worse than not chaining."""
    project = await _project(session)
    video = await _video(session, project.id)
    other = await _video(session, project.id, uri="local:///other.mp4")
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id), str(other.id)],
        final_assets=[_video_asset("local:///merged.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
    )

    assert await recent_edits(session, project.id, video.id) == []


async def test_recent_edits_ignores_jobs_with_no_chainable_video_output(session) -> None:
    """A final stage that produced no video asset at all must not count.

    Kept for the degenerate case, but note it does NOT describe what real
    analysis workers emit -- see the two tests below for that.
    """
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[{"kind": "transcript", "uri": "local:///t.json"}],
        completed_at=dt.datetime.now(dt.UTC),
    )

    assert await recent_edits(session, project.id, video.id) == []


async def test_analysis_only_job_is_not_an_edit(session) -> None:
    """The realistic detect_scenes payload, which the test above does not
    produce. SceneDetectionWorker._finish inserts an Asset(kind=VIDEO,
    uri=<its own input>) when nothing was carried, so an analysis-only job
    DOES emit a video asset -- a real one, pointing at the unedited source.

    Before this was fixed it was recorded as an edit: it burned one of the
    two version slots and told the planner "already applied: detect_scenes"
    about a video nothing had edited.
    """
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        stage_zero_video_uris=["local:///orig.mp4"],
        final_assets=[
            _video_asset("local:///orig.mp4"),  # passed through, unchanged
            {"kind": "scenes", "uri": "local:///scenes.json"},
        ],
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["detect_scenes"],
    )

    assert await recent_edits(session, project.id, video.id) == []


async def test_transcribe_only_job_is_not_an_edit(session) -> None:
    """Same shape from TranscribeWorker: video passed through untouched
    alongside the transcript and srt it produced."""
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        stage_zero_video_uris=["local:///orig.mp4"],
        final_assets=[
            _video_asset("local:///orig.mp4"),
            {"kind": "transcript", "uri": "local:///t.json"},
            {"kind": "srt", "uri": "local:///t.srt"},
        ],
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["transcribe"],
    )

    assert await recent_edits(session, project.id, video.id) == []


async def test_a_real_edit_is_still_found_when_input_uris_are_recorded(session) -> None:
    """The guard must not swallow genuine edits: same payload shape, but
    the produced video differs from the input."""
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        stage_zero_video_uris=["local:///orig.mp4"],
        final_assets=[_video_asset("local:///trimmed.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["trim"],
    )

    edits = await recent_edits(session, project.id, video.id)

    assert [edit.uri for edit in edits] == ["local:///trimmed.mp4"]


async def test_recent_edits_orders_newest_first(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id)
    now = dt.datetime.now(dt.UTC)
    first = await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///first-trim.mp4")],
        completed_at=now - dt.timedelta(minutes=10),
    )
    newest = await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///second-trim.mp4")],
        completed_at=now,
    )

    edits = await recent_edits(session, project.id, video.id)

    assert [e.job_id for e in edits] == [newest.id, first.id]
    assert edits[0].uri == "local:///second-trim.mp4"
    assert edits[1].uri == "local:///first-trim.mp4"


async def test_recent_edits_is_bounded_by_limit(session) -> None:
    """Three edits exist, but the window is the newest two only -- this is
    a bounded revert window, not a full version history."""
    project = await _project(session)
    video = await _video(session, project.id)
    now = dt.datetime.now(dt.UTC)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///v1.mp4")],
        completed_at=now - dt.timedelta(minutes=20),
    )
    v2 = await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///v2.mp4")],
        completed_at=now - dt.timedelta(minutes=10),
    )
    v3 = await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///v3.mp4")],
        completed_at=now,
    )

    edits = await recent_edits(session, project.id, video.id, limit=2)

    assert [e.job_id for e in edits] == [v3.id, v2.id]


async def test_recent_edits_shorter_than_limit_when_history_is_shorter(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///only-edit.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
    )

    edits = await recent_edits(session, project.id, video.id, limit=2)

    assert len(edits) == 1


async def test_with_edit_history_noop_when_never_edited(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id)
    contexts = [
        VideoContext(handle="video_1", video_id=str(video.id), display_name="orig.mp4", uri=video.storage_uri)
    ]

    result = await with_edit_history(session, project.id, contexts)

    assert result == contexts


_FAKE_MEASUREMENTS = {
    "local:///trimmed.mp4": {
        "duration_seconds": 75.0,
        "resolution": "1920x1080",
        "orientation": "landscape",
    },
    "local:///cropped.mp4": {
        "duration_seconds": 70.0,
        "resolution": "1080x1920",
        "orientation": "portrait",
    },
}


async def _fake_measure(uri: str) -> dict | None:
    return _FAKE_MEASUREMENTS.get(uri)


async def test_with_edit_history_repoints_uri_and_adds_original_handle(
    session, monkeypatch
) -> None:
    """Exactly one prior edit: two handles come back (no `_previous`, since
    there is nothing before the one edit that isn't the original), and the
    edited handle's facts are the fresh measurement, not the original's."""
    monkeypatch.setattr("backend.services.planner_context.measure", _fake_measure)
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///trimmed.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
    )
    contexts = [
        VideoContext(
            handle="video_1",
            video_id=str(video.id),
            display_name="orig.mp4",
            uri=video.storage_uri,
            duration_seconds=140.0,  # the ORIGINAL's (stale, pre-edit) duration
        )
    ]

    result = await with_edit_history(session, project.id, contexts)

    assert [c.handle for c in result] == ["video_1", "video_1_original"]

    edited, original = result
    assert edited.uri == "local:///trimmed.mp4"
    assert edited.edit_note == _edit_note(["trim"], measured=True)
    assert "trim" in edited.edit_note and "do NOT include" in edited.edit_note
    # The measured (post-trim) duration, not the original 140s carried over.
    assert edited.duration_seconds == 75.0
    assert edited.resolution == "1920x1080"
    assert edited.orientation == "landscape"
    assert original.uri == video.storage_uri
    assert original.video_id == str(video.id)
    assert original.duration_seconds == 140.0  # original's own facts, untouched
    assert original.edit_note is None


async def test_with_edit_history_three_states_after_two_edits(session, monkeypatch) -> None:
    """Two prior edits: video_1 (latest), video_1_previous (the one before
    it), video_1_original (untouched upload) -- the full 3-state window,
    each carrying its own freshly measured facts."""
    monkeypatch.setattr("backend.services.planner_context.measure", _fake_measure)
    project = await _project(session)
    video = await _video(session, project.id)
    now = dt.datetime.now(dt.UTC)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///trimmed.mp4")],
        completed_at=now - dt.timedelta(minutes=10),
    )
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///cropped.mp4")],
        completed_at=now,
    )
    contexts = [
        VideoContext(handle="video_1", video_id=str(video.id), display_name="orig.mp4", uri=video.storage_uri)
    ]

    result = await with_edit_history(session, project.id, contexts)

    assert [c.handle for c in result] == ["video_1", "video_1_previous", "video_1_original"]

    latest, previous, original = result
    assert latest.uri == "local:///cropped.mp4"
    assert latest.edit_note == _edit_note(["trim"], measured=True)
    assert latest.duration_seconds == 70.0
    assert latest.orientation == "portrait"
    assert previous.uri == "local:///trimmed.mp4"
    assert previous.video_id == str(video.id)
    assert previous.duration_seconds == 75.0
    assert previous.orientation == "landscape"
    # _previous now carries the same kind of note as the primary handle --
    # a user reverting to it and building further needs the same warning.
    assert previous.edit_note == _edit_note(["trim"], measured=True)
    assert original.uri == video.storage_uri


async def test_with_edit_history_falls_back_to_stale_facts_when_measurement_fails(
    session, monkeypatch
) -> None:
    """measure() failing (missing file, ffprobe crash) must not blank out
    what was already known, and must say so rather than claiming freshness
    it doesn't have."""

    async def _always_fails(uri: str) -> None:
        return None

    monkeypatch.setattr("backend.services.planner_context.measure", _always_fails)
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///trimmed.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
    )
    contexts = [
        VideoContext(
            handle="video_1",
            video_id=str(video.id),
            display_name="orig.mp4",
            uri=video.storage_uri,
            duration_seconds=140.0,
            resolution="1920x1080",
        )
    ]

    result = await with_edit_history(session, project.id, contexts)
    edited = result[0]

    assert edited.uri == "local:///trimmed.mp4"
    assert edited.edit_note == _edit_note(["trim"], measured=False)
    # Stale, but present -- better than blanking out what little was known.
    assert edited.duration_seconds == 140.0
    assert edited.resolution == "1920x1080"


async def test_with_edit_history_note_names_stages_and_warns_against_repeating(
    session, monkeypatch
) -> None:
    """Regression test for the real-room bug: "trim 0:40-1:10" approved, then
    "remove the last 5 secs of this too" re-included remove_segment on top of
    the already-cut clip, cutting a second unrelated 30s instead of trimming
    5 more -- 105.8s became 75.8s, not the stated 100.8s. The note must name
    the stages already applied and explicitly say not to repeat them."""

    async def _measured(uri: str) -> dict:
        return {"duration_seconds": 105.8, "resolution": None, "orientation": None}

    monkeypatch.setattr("backend.services.planner_context.measure", _measured)
    project = await _project(session)
    video = await _video(session, project.id)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        final_assets=[_video_asset("local:///cut.mp4")],
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["remove_segment", "trim"],
    )
    contexts = [
        VideoContext(handle="video_1", video_id=str(video.id), display_name="clip.mp4", uri=video.storage_uri)
    ]

    result = await with_edit_history(session, project.id, contexts)
    edited = result[0]

    assert "remove_segment, trim" in edited.edit_note
    assert "do NOT include remove_segment, trim again" in edited.edit_note
    assert "_previous" in edited.edit_note and "_original" in edited.edit_note


# --- video_chain.measure ----------------------------------------------------


async def test_measure_returns_none_when_storage_object_missing(tmp_path, monkeypatch) -> None:
    storage = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: storage)

    assert await measure("local:///does-not-exist.mp4") is None


async def test_measure_extracts_real_metadata_from_sample_video(
    ffprobe_available, tmp_path, monkeypatch
) -> None:
    """The one test here that runs real ffprobe -- gated the same way
    test_video_analysis_worker.py's equivalent is."""
    sample = _FIXTURES_DIR / "sample.mp4"
    storage = LocalDiskStorage(tmp_path)
    uri = storage.put(sample.read_bytes(), suggested_name="sample.mp4")
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: storage)

    metadata = await measure(uri)

    assert metadata == {
        "duration_seconds": 3.0,
        "fps": 30.0,
        "width": 1280,
        "height": 720,
        "resolution": "1280x720",
        "orientation": "landscape",
        "codec": "h264",
    }


# --- analysis_assets / summarize_analysis --------------------------------
#
# The channel that lets a planner see what a room already learned about a
# video. Before this existed, analysis output travelled only within one
# compiled job and vanished at the turn boundary.


async def test_analysis_assets_returns_non_video_outputs(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        stage_zero_video_uris=["local:///orig.mp4"],
        final_assets=[
            _video_asset("local:///orig.mp4"),
            {"kind": "scenes", "uri": "local:///scenes.json"},
        ],
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["detect_scenes"],
    )

    found = await analysis_assets(session, project.id, video.id)

    assert [(a.kind, a.uri) for a in found] == [("scenes", "local:///scenes.json")]


async def test_analysis_assets_keeps_only_the_newest_of_each_kind(session) -> None:
    """Supersede, not accumulate: re-running detect_scenes at a different
    threshold replaces the older scene list rather than sitting beside it."""
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    now = dt.datetime.now(dt.UTC)
    for uri, when in (
        ("local:///scenes-old.json", now - dt.timedelta(hours=2)),
        ("local:///scenes-new.json", now),
    ):
        await _completed_job(
            session,
            project.id,
            stage_zero_video_ids=[str(video.id)],
            stage_zero_video_uris=["local:///orig.mp4"],
            final_assets=[_video_asset("local:///orig.mp4"), {"kind": "scenes", "uri": uri}],
            completed_at=when,
            workflow=["detect_scenes"],
        )

    found = await analysis_assets(session, project.id, video.id)

    assert [a.uri for a in found] == ["local:///scenes-new.json"]


async def test_analysis_assets_survives_older_than_the_edit_window(session) -> None:
    """recent_edits caps at 2 deliberately; analysis must not. A transcript
    from five edits ago is still the transcript."""
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    now = dt.datetime.now(dt.UTC)
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        stage_zero_video_uris=["local:///orig.mp4"],
        final_assets=[
            _video_asset("local:///orig.mp4"),
            {"kind": "transcript", "uri": "local:///t.json"},
        ],
        completed_at=now - dt.timedelta(hours=5),
        workflow=["transcribe"],
    )
    for i in range(3):
        await _completed_job(
            session,
            project.id,
            stage_zero_video_ids=[str(video.id)],
            stage_zero_video_uris=["local:///orig.mp4"],
            final_assets=[_video_asset(f"local:///edit{i}.mp4")],
            completed_at=now - dt.timedelta(hours=i),
            workflow=["trim"],
        )

    found = await analysis_assets(session, project.id, video.id)

    assert [a.kind for a in found] == ["transcript"]


async def test_analysis_assets_ignores_previews(session) -> None:
    project = await _project(session)
    video = await _video(session, project.id, uri="local:///orig.mp4")
    await _completed_job(
        session,
        project.id,
        stage_zero_video_ids=[str(video.id)],
        stage_zero_video_uris=["local:///orig.mp4"],
        final_assets=[
            _video_asset("local:///orig.mp4"),
            {"kind": "scenes", "uri": "local:///scenes.json"},
        ],
        preview=True,
        completed_at=dt.datetime.now(dt.UTC),
        workflow=["detect_scenes"],
    )

    assert await analysis_assets(session, project.id, video.id) == []


def _analysis(kind: str, uri: str) -> VideoAnalysis:
    return VideoAnalysis(
        kind=kind, uri=uri, job_id=uuid.uuid4(), produced_at=dt.datetime.now(dt.UTC)
    )


def test_summarize_scenes_lists_timestamps_and_counts(tmp_path, monkeypatch) -> None:
    storage = LocalDiskStorage(tmp_path)
    payload = json.dumps({"cuts": [{"time": 0.0}, {"time": 12.4}, {"time": 91.2}]}).encode()
    uri = storage.put(payload, suggested_name="scenes.json")
    monkeypatch.setattr("backend.services.video_chain.get_storage", lambda: storage)

    assert summarize_analysis(_analysis("scenes", uri)) == "scenes: 3 cuts at 0:00, 0:12, 1:31"


def test_summarize_scenes_caps_long_lists(tmp_path, monkeypatch) -> None:
    """An hour of footage can carry hundreds of cuts, and this text goes
    into every later prompt for the room."""
    storage = LocalDiskStorage(tmp_path)
    payload = json.dumps({"cuts": [{"time": float(i)} for i in range(40)]}).encode()
    uri = storage.put(payload, suggested_name="scenes.json")
    monkeypatch.setattr("backend.services.video_chain.get_storage", lambda: storage)

    summary = summarize_analysis(_analysis("scenes", uri))

    assert "40 cuts" in summary
    assert "first 10 of 40" in summary
    assert summary.count(",") < 12


def test_summarize_transcript_reports_words_and_a_capped_excerpt(tmp_path, monkeypatch) -> None:
    storage = LocalDiskStorage(tmp_path)
    payload = json.dumps({"text": "hello world " * 200}).encode()
    uri = storage.put(payload, suggested_name="t.json")
    monkeypatch.setattr("backend.services.video_chain.get_storage", lambda: storage)

    summary = summarize_analysis(_analysis("transcript", uri))

    assert "400 words" in summary
    assert len(summary) < 600  # never the raw transcript


def test_summarize_degrades_when_the_object_is_unreadable(monkeypatch) -> None:
    """A summary is an enrichment; a missing object must not break the turn
    that asked for it."""
    storage = LocalDiskStorage(Path(__file__).parent)  # real dir, missing key
    monkeypatch.setattr("backend.services.video_chain.get_storage", lambda: storage)

    summary = summarize_analysis(_analysis("scenes", "local://nope.json"))

    assert "could not be read" in summary


def test_summarize_reports_zero_cuts_as_a_finding_not_a_failure(tmp_path, monkeypatch) -> None:
    storage = LocalDiskStorage(tmp_path)
    uri = storage.put(json.dumps({"cuts": []}).encode(), suggested_name="scenes.json")
    monkeypatch.setattr("backend.services.video_chain.get_storage", lambda: storage)

    assert "no cuts detected" in summarize_analysis(_analysis("scenes", uri))
