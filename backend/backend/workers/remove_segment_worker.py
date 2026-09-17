"""RemoveSegmentWorker — cut a middle piece out of a video and rejoin what's
left (Phase 5E+).

`trim` (trim_worker.py) keeps only the range between start and end; this is
deliberately its complement -- keep everything *except* that range. Dropping
the head or tail alone is already `trim`'s job (`trim(end=X)` /
`trim(start=X)`); this capability exists for the shape `trim` cannot express
at all: two surviving pieces that must be rejoined into one clip. Built
after a live mix-up where "trim from 0:40 to 1:10" was read (correctly, per
`trim`'s own contract) as "keep 0:40-1:10" when what was wanted was "remove
0:40-1:10, keep the rest" -- see both capabilities' descriptions in
capability_registry.py, which is what actually steers the planner between
them.

Single input, so this is simpler than merge_worker.py's multi-source
concat: both surviving pieces already share one frame size, one frame
rate, and one audio-presence, so none of merge's canvas/fps normalisation
is needed. Still can't use media.process_video, which applies one linear
filter chain to one continuous stream -- here two disjoint ranges of the
*same* input must be cut and concatenated, which needs a filter_complex.
Reusing `[0:v]`/`[0:a]` as the input to two independent filter chains in
one filter_complex works with no `split`/`asplit` needed (verified against
the ffmpeg on this machine before writing this).
"""

from typing import Any

from backend.core.config import get_settings
from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    AssetKind,
    InvalidMediaParamsError,
    PREVIEW_OUTPUT_ARGS,
    PREVIEW_SCALE_FILTER,
    assets_payload,
    forward_assets,
    is_preview,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    probe,
    put_asset,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
)

# Below this, either the removed range or a surviving side isn't something
# anyone actually asked for -- almost always a start/end the planner got
# backwards, or a range that consumes an entire (near-zero-length) side.
_MIN_DURATION_SECONDS = 0.1


class RemoveSegmentWorker(Worker):
    name = "remove_segment"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        start, end = _parse_window(stage_params(message))
        preview = is_preview(message)

        with materialize_to_tempfile(resolve_input_uri(message, previous_output)) as source:
            probe_data = await probe(source)
            duration = _duration(probe_data)
            has_audio = _has_audio(probe_data)
            end = min(end, duration)

            keep_before = start > _MIN_DURATION_SECONDS
            keep_after = duration - end > _MIN_DURATION_SECONDS
            if not keep_before and not keep_after:
                raise InvalidMediaParamsError(
                    "remove_segment's range covers the entire video -- nothing would "
                    "be left"
                )

            filtergraph, outputs = _build(start, end, keep_before, keep_after, has_audio, preview)

            args = ["-i", str(source), "-filter_complex", filtergraph, *outputs]
            args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
            if preview:
                args += list(PREVIEW_OUTPUT_ARGS)
            else:
                args += ["-preset", get_settings().video_encode_preset]

            with output_tempfile(".mp4") as destination:
                await run_ffmpeg([*args, str(destination)])
                if not destination.is_file() or destination.stat().st_size == 0:
                    raise InvalidMediaParamsError("remove_segment produced no output")
                produced = put_asset(destination, kind=AssetKind.VIDEO)

        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _duration(probe_data: dict[str, Any]) -> float:
    return max(0.0, float(probe_data.get("format", {}).get("duration") or 0.0))


def _has_audio(probe_data: dict[str, Any]) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe_data.get("streams", []))


def _seconds(name: str, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InvalidMediaParamsError(
            f"remove_segment's '{name}' must be a number of seconds; got {raw!r}"
        )
    value = float(raw)
    if value < 0:
        raise InvalidMediaParamsError(
            f"remove_segment's '{name}' cannot be negative; got {value}"
        )
    return value


def _parse_window(params: dict[str, Any]) -> tuple[float, float]:
    unknown = set(params) - {"start", "end"}
    if unknown:
        raise InvalidMediaParamsError(
            f"remove_segment got unknown parameter(s): {', '.join(sorted(unknown))}"
        )
    # Both required, unlike trim -- an open-ended remove ("everything after
    # X") is already trim(end=X)/trim(start=X); this capability's whole
    # reason to exist is a bounded middle range with survivors on both sides.
    if "start" not in params or "end" not in params:
        raise InvalidMediaParamsError("remove_segment needs both a start and an end")

    start = _seconds("start", params["start"])
    end = _seconds("end", params["end"])
    if end - start < _MIN_DURATION_SECONDS:
        raise InvalidMediaParamsError(
            f"remove_segment's end ({end}) must be at least {_MIN_DURATION_SECONDS}s "
            f"after its start ({start})"
        )
    return start, end


def _build(
    start: float,
    end: float,
    keep_before: bool,
    keep_after: bool,
    has_audio: bool,
    preview: bool,
) -> tuple[str, list[str]]:
    """Two trims of the same input, rejoined -- or, in the degenerate case
    where the requested range reaches one edge of the video, just the one
    surviving trim with no concat (concat needs two real inputs)."""
    video_pad = "vraw" if preview else "outv"
    v_chains: list[str] = []
    a_chains: list[str] = []

    if keep_before and keep_after:
        v_chains.append(f"[0:v]trim=start=0:end={start},setpts=PTS-STARTPTS[v0]")
        v_chains.append(f"[0:v]trim=start={end},setpts=PTS-STARTPTS[v1]")
        v_chains.append(f"[v0][v1]concat=n=2:v=1:a=0[{video_pad}]")
        if has_audio:
            a_chains.append(f"[0:a]atrim=start=0:end={start},asetpts=PTS-STARTPTS[a0]")
            a_chains.append(f"[0:a]atrim=start={end},asetpts=PTS-STARTPTS[a1]")
            a_chains.append("[a0][a1]concat=n=2:v=0:a=1[outa]")
    elif keep_before:
        v_chains.append(f"[0:v]trim=start=0:end={start},setpts=PTS-STARTPTS[{video_pad}]")
        if has_audio:
            a_chains.append(f"[0:a]atrim=start=0:end={start},asetpts=PTS-STARTPTS[outa]")
    else:
        v_chains.append(f"[0:v]trim=start={end},setpts=PTS-STARTPTS[{video_pad}]")
        if has_audio:
            a_chains.append(f"[0:a]atrim=start={end},asetpts=PTS-STARTPTS[outa]")

    if preview:
        v_chains.append(f"[{video_pad}]{PREVIEW_SCALE_FILTER}[outv]")

    filtergraph = ";".join(v_chains + a_chains)
    outputs = ["-map", "[outv]"]
    if has_audio:
        outputs += ["-map", "[outa]"]
    return filtergraph, outputs
