"""MergeWorker — concatenate several clips into one (Phase 5E).

The one capability in Phase 5 that takes more than one input, and the only
one that can't use media.process_video (which resolves exactly one input
video). It drives run_ffmpeg directly instead.

**First stage only, by construction.** Setu forwards the original
job-submission payload to every stage, so `video_uris` holds the project's
real uploads only at stage 0; from stage 1 onward the true input is
whatever the previous stage produced, which is a single video. A merge
placed later in a workflow would silently concatenate the *original*
uploads and discard every edit before it -- so it refuses rather than
doing that. Mid-chain merge needs multi-input dispatch in the execution
engine and is deferred (architecture doc, backlog item 6).

Two real-world messes are handled rather than assumed away:
  - clips of differing sizes, which the concat filter rejects outright:
    everything is scaled and padded onto the first clip's canvas.
  - clips where only some have sound, which would otherwise produce a
    stream-count mismatch: silence is synthesised for the ones missing it.
"""

from pathlib import Path
from typing import Any

from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    AssetKind,
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    probe,
    put_asset,
    run_ffmpeg,
    stage_video_uris,
    video_stream,
)

_SILENT_AUDIO = "anullsrc=channel_layout=stereo:sample_rate=44100"


class MergeWorker(Worker):
    name = "merge"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        if message.stage != 0:
            raise InvalidMediaParamsError(
                "merge can only run as the first stage of a workflow: later stages "
                "receive one video from the stage before them, so a merge here would "
                "concatenate the original uploads and silently discard the edits "
                "already applied"
            )

        sources = stage_video_uris(message)
        if len(sources) < 2:
            raise InvalidMediaParamsError(
                f"merge needs at least two videos to join; got {len(sources)}"
            )

        # ExitStack would flatten this, but the temp files must all stay
        # alive across the single ffmpeg call, so they're materialised
        # together and released together.
        from contextlib import ExitStack

        with ExitStack() as stack:
            paths = [
                stack.enter_context(materialize_to_tempfile(uri)) for uri in sources
            ]
            probes = [await probe(path) for path in paths]
            width, height = _canvas(probes[0])
            fps = _framerate(probes[0])
            has_audio = [_has_audio(data) for data in probes]
            durations = [_duration(data) for data in probes]

            args, filtergraph, outputs = _build(
                paths, width, height, fps, has_audio, durations
            )
            with output_tempfile(".mp4") as destination:
                await run_ffmpeg(
                    [
                        *args,
                        "-filter_complex", filtergraph,
                        *outputs,
                        "-c:v", "libx264",
                        "-pix_fmt", "yuv420p",
                        str(destination),
                    ]
                )
                if not destination.is_file() or destination.stat().st_size == 0:
                    raise InvalidMediaParamsError("merge produced no output")
                produced = put_asset(destination, kind=AssetKind.VIDEO)

        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _has_audio(probe_data: dict[str, Any]) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe_data.get("streams", []))


def _duration(probe_data: dict[str, Any]) -> float:
    """How long a clip runs, used to size its synthesised silence.

    Getting this wrong is not cosmetic: concat lines segments up stream by
    stream, so a silent track shorter than its own video truncates the
    joined result and leaves the two streams disagreeing about the total.
    """
    return max(0.1, float(probe_data.get("format", {}).get("duration") or 0.0))


def _framerate(first: dict[str, Any]) -> str:
    """Frame rate every clip is conformed to, taken from the first.

    concat joins streams without reconciling frame rate, so clips shot at
    different rates come out with the wrong total duration -- joining 30fps
    and 15fps footage measurably lost ~70ms of the second clip before this
    was normalised. ffprobe reports the rate as a "num/den" string, which
    ffmpeg's fps filter accepts directly.
    """
    raw = video_stream(first).get("r_frame_rate") or "30/1"
    return raw if "/" in raw and not raw.endswith("/0") else "30/1"


def _canvas(first: dict[str, Any]) -> tuple[int, int]:
    """Everything is normalised onto the first clip's frame size.

    The concat filter refuses inputs whose dimensions differ, and picking
    the first clip is the least surprising rule: the output looks like
    what the user listed first, rather than like whichever clip happened
    to be largest.
    """
    stream = video_stream(first)
    width, height = int(stream["width"]), int(stream["height"])
    return max(2, width - width % 2), max(2, height - height % 2)


def _build(
    paths: list[Path],
    width: int,
    height: int,
    fps: str,
    has_audio: list[bool],
    durations: list[float],
) -> tuple[list[str], str, list[str]]:
    """Assemble the ffmpeg invocation: inputs, filtergraph, output maps."""
    args: list[str] = []
    for path in paths:
        args += ["-i", str(path)]

    # One silent source per clip that lacks audio, appended after the real
    # inputs so their indices stay stable. Each is cut to that clip's own
    # duration -- a fixed length would truncate the joined output to the
    # shortest silence.
    silent_index: dict[int, int] = {}
    if any(has_audio):
        for position, present in enumerate(has_audio):
            if not present:
                silent_index[position] = len(paths) + len(silent_index)
                args += ["-f", "lavfi", "-t", f"{durations[position]}", "-i", _SILENT_AUDIO]

    chains: list[str] = []
    segments: list[str] = []
    for position in range(len(paths)):
        # force_original_aspect_ratio=decrease fits the clip inside the
        # canvas without distorting it; pad fills the remainder; setsar
        # normalises pixel aspect ratio, which concat also insists match.
        chains.append(
            f"[{position}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{position}]"
        )
        segments.append(f"[v{position}]")
        if any(has_audio):
            source = position if has_audio[position] else silent_index[position]
            chains.append(f"[{source}:a]aresample=44100,asetpts=PTS-STARTPTS[a{position}]")
            segments.append(f"[a{position}]")

    audio_flag = 1 if any(has_audio) else 0
    concat = (
        f"{''.join(segments)}concat=n={len(paths)}:v=1:a={audio_flag}[outv]"
        + ("[outa]" if audio_flag else "")
    )
    filtergraph = ";".join([*chains, concat])

    outputs = ["-map", "[outv]"]
    if audio_flag:
        outputs += ["-map", "[outa]"]
    return args, filtergraph, outputs
