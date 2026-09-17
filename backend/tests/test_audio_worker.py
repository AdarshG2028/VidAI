"""AudioWorker (Phase 5D).

Uses tests/fixtures/sample_with_audio.mp4 rather than sample.mp4, which is
video-only. That fixture is deliberately built to exercise both operations:
4s long, ~-44 LUFS (far below the -16 target), with a 1s silent lead-in and
a 1s silent tail around a 2s tone.

Regenerate it with:
  ffmpeg -f lavfi -i "testsrc=size=320x240:rate=15:duration=4" \\
    -filter_complex "aevalsrc=0:d=1[s1]; sine=frequency=440:duration=2:sample_rate=44100[t]; \\
                     aevalsrc=0:d=1[s2]; [s1][t][s2]concat=n=3:v=0:a=1,volume=0.08[a]" \\
    -map 0:v -map "[a]" -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \\
    tests/fixtures/sample_with_audio.mp4
"""

import re
import uuid
from pathlib import Path

import pytest

from backend.storage.local import LocalDiskStorage
from backend.workers.audio_worker import AudioWorker
from backend.workers.base import PermanentError, StageMessage
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    materialize_to_tempfile,
    previous_assets,
    primary_video,
    probe,
    run_ffmpeg,
)

_SAMPLE = Path(__file__).parent / "fixtures" / "sample_with_audio.mp4"


@pytest.fixture
def storage(tmp_path, monkeypatch) -> LocalDiskStorage:
    disk = LocalDiskStorage(tmp_path)
    monkeypatch.setattr("backend.workers.media.get_storage", lambda: disk)
    return disk


def _message(source_uri: str, params: dict, stage: int = 0) -> StageMessage:
    return StageMessage(
        job_id=uuid.uuid4(),
        stage=stage,
        workflow=["audio"],
        payload={"stage_params": {str(stage): {"params": params, "video_uris": [source_uri]}}},
    )


async def _stream_durations(uri: str) -> dict[str, float]:
    with materialize_to_tempfile(uri) as path:
        data = await probe(path)
    return {s["codec_type"]: float(s.get("duration") or 0.0) for s in data["streams"]}


async def _loudness(uri: str) -> float:
    with materialize_to_tempfile(uri) as path:
        stderr = await run_ffmpeg(
            ["-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"]
        )
    match = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', stderr)
    assert match, "could not read loudness from ffmpeg output"
    return float(match.group(1))


async def _run(storage: LocalDiskStorage, params: dict) -> str:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")
    payload = await AudioWorker().process(_message(source, params), None)
    return primary_video(previous_assets(payload)).uri


# --- parameter validation (no ffmpeg needed) -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},                                              # nothing asked for
        {"normalize": False},                            # explicitly nothing
        {"remove_silence": True, "preserve_music": True},  # the only action is suppressed
        {"normalize": "yes"},                            # not a bool
        {"remove_silence": 1},
        {"denoise": True},                               # unknown param
    ],
)
async def test_rejects_unusable_requests_permanently(storage, params: dict) -> None:
    source = storage.put(b"x", suggested_name="x.mp4")

    with pytest.raises(InvalidMediaParamsError) as exc_info:
        await AudioWorker().process(_message(source, params), None)

    assert isinstance(exc_info.value, PermanentError)


# --- normalise -------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalize_raises_a_quiet_clip_towards_the_target(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")
    before = await _loudness(source)

    after = await _loudness(await _run(storage, {"normalize": True}))

    assert before < -40, "fixture should start far below target"
    assert after == pytest.approx(-16, abs=2.0), f"expected ~-16 LUFS, got {after}"


@pytest.mark.asyncio
async def test_normalize_alone_does_not_change_duration(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Loudness is not trimming — asking only to normalise must leave the
    clip exactly as long as it was."""
    durations = await _stream_durations(await _run(storage, {"normalize": True}))

    assert durations["video"] == pytest.approx(4.0, abs=0.2)


# --- silence trimming ------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_silence_trims_the_silent_head_and_tail(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """4s clip = 1s silence + 2s tone + 1s silence, so trimming leaves ~2s."""
    durations = await _stream_durations(await _run(storage, {"remove_silence": True}))

    assert durations["video"] == pytest.approx(2.0, abs=0.3)


@pytest.mark.asyncio
async def test_remove_silence_keeps_audio_and_video_in_sync(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """The failure this worker's whole design exists to avoid.

    ffmpeg's `silenceremove` filter shortens only the audio stream. Used
    directly, the video would still be 4s while the audio became 2s — the
    clip would drift out of sync and end on a frozen tail. Matched
    trim/atrim filters are what keep the two equal.
    """
    durations = await _stream_durations(await _run(storage, {"remove_silence": True}))

    assert abs(durations["video"] - durations["audio"]) < 0.2, (
        f"streams drifted apart: {durations}"
    )


@pytest.mark.asyncio
async def test_preserve_music_suppresses_silence_removal(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """No content classification happens — preserve_music simply means
    'leave the pauses alone', so the clip keeps its full length."""
    durations = await _stream_durations(
        await _run(storage, {"normalize": True, "remove_silence": True, "preserve_music": True})
    )

    assert durations["video"] == pytest.approx(4.0, abs=0.2)


@pytest.mark.asyncio
async def test_both_operations_together(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Normalisation is measured after trimming, so the silence doesn't
    drag the measured loudness down."""
    output = await _run(storage, {"normalize": True, "remove_silence": True})

    durations = await _stream_durations(output)
    assert durations["video"] == pytest.approx(2.0, abs=0.3)
    assert await _loudness(output) == pytest.approx(-16, abs=2.5)


@pytest.mark.asyncio
async def test_a_clip_with_no_silence_is_left_at_full_length(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    """Nothing to trim must mean no trim, not a clip mangled down to the
    minimum-length guard."""
    source_bytes = _SAMPLE.read_bytes()
    source = storage.put(source_bytes, suggested_name="clip.mp4")
    # Trim the fixture down to just its tone, leaving no silence at all.
    with materialize_to_tempfile(source) as path:
        from backend.workers.media import output_tempfile, put_asset

        with output_tempfile(".mp4") as dest:
            await run_ffmpeg(
                ["-i", str(path), "-ss", "1", "-to", "3", "-c:v", "libx264",
                 "-c:a", "aac", str(dest)]
            )
            toneonly = put_asset(dest).uri

    payload = await AudioWorker().process(
        _message(toneonly, {"remove_silence": True}), None
    )
    durations = await _stream_durations(primary_video(previous_assets(payload)).uri)

    assert durations["video"] == pytest.approx(2.0, abs=0.3)


# --- chaining --------------------------------------------------------------


@pytest.mark.asyncio
async def test_chains_and_forwards_other_assets(
    ffmpeg_available, ffprobe_available, storage
) -> None:
    source = storage.put(_SAMPLE.read_bytes(), suggested_name="clip.mp4")
    first = await AudioWorker().process(_message(source, {"normalize": True}), None)
    srt = Asset(kind=AssetKind.SRT, uri="local://captions.srt")
    previous = {"assets": [*first["assets"], srt.to_dict()]}

    payload = await AudioWorker().process(
        _message(source, {"remove_silence": True}, stage=1), previous
    )

    assets = previous_assets(payload)
    assert srt in assets
    durations = await _stream_durations(primary_video(assets).uri)
    assert durations["video"] == pytest.approx(2.0, abs=0.3), "should trim the chained input"
