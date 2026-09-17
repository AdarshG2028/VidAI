"""TrimWorker — cut a video down to a time range (Phase 5E).

The most commonly wanted edit there is, and the one whose absence was the
difference between this product being a video *enhancer* and a video
*editor*: everything in 5B-5D changes how a clip looks or sounds, and none
of it can drop the boring middle.

Uses matched trim/atrim filters rather than ffmpeg's `-ss`/`-to` input
seeking. Input seeking lands on the nearest keyframe, so with the stream
copy it invites it would silently cut somewhere other than asked --
possibly seconds out on footage with sparse keyframes. Filters are
frame-accurate, at the cost of re-encoding.

Both stream filters are applied unconditionally: ffmpeg ignores `-af` on a
video with no audio track (verified), so this needs no probe to find out
whether one exists.
"""

from typing import Any

from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    previous_assets,
    process_video,
    stage_params,
)

# Below this, the result isn't a clip anyone asked for -- almost always a
# start/end the planner got backwards or misread.
_MIN_DURATION_SECONDS = 0.1


class TrimWorker(Worker):
    name = "trim"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        start, end = _parse_window(stage_params(message))

        # trim's own start/end are absolute times in the input; setpts
        # rebases the output to begin at zero, so the result doesn't keep
        # a leading gap where the removed head used to be.
        video = ["trim=" + _range(start, end), "setpts=PTS-STARTPTS"]
        audio = ["atrim=" + _range(start, end), "asetpts=PTS-STARTPTS"]

        produced = await process_video(
            message, previous_output, video_filters=video, audio_filters=audio
        )
        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _seconds(name: str, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InvalidMediaParamsError(
            f"trim's '{name}' must be a number of seconds; got {raw!r}"
        )
    value = float(raw)
    if value < 0:
        raise InvalidMediaParamsError(f"trim's '{name}' cannot be negative; got {value}")
    return value


def _parse_window(params: dict[str, Any]) -> tuple[float | None, float | None]:
    unknown = set(params) - {"start", "end"}
    if unknown:
        raise InvalidMediaParamsError(
            f"trim got unknown parameter(s): {', '.join(sorted(unknown))}"
        )

    start = _seconds("start", params["start"]) if "start" in params else None
    end = _seconds("end", params["end"]) if "end" in params else None

    if start is None and end is None:
        raise InvalidMediaParamsError("trim needs a start, an end, or both")
    if start is not None and end is not None and end - start < _MIN_DURATION_SECONDS:
        raise InvalidMediaParamsError(
            f"trim's end ({end}) must be at least {_MIN_DURATION_SECONDS}s after its "
            f"start ({start})"
        )
    return start, end


def _range(start: float | None, end: float | None) -> str:
    """Only the bounds that were given.

    Omitting one is meaningful rather than a default worth filling in:
    `start` alone runs to the end of the clip, `end` alone runs from the
    beginning, and neither requires knowing how long the input is.
    """
    parts = []
    if start is not None:
        parts.append(f"start={start}")
    if end is not None:
        parts.append(f"end={end}")
    return ":".join(parts)
