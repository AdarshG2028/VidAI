"""The rest of 5B's spatial transforms: resize, rotate, flip, pad.

Grouped in one module the way stage_workers.py already groups its related
workers, rather than four ~40-line files. CropWorker stays in
crop_worker.py: it was the sub-phase's template and is referenced as such,
so moving it now would be churn for symmetry's sake.

Separate *capabilities* though, not one polymorphic "transform" — these are
order-sensitive against each other (cropping then scaling is not the same
picture as scaling then cropping), and the proposal's workflow list is
where that order is already expressed. Contrast ColorWorker, whose
adjustments are order-insensitive and so are grouped into one stage.

Every h264 dimension here is forced even: yuv420p chroma subsampling
cannot represent odd width or height, and ffmpeg errors out rather than
rounding for you.
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

# Upper bound on requested pixel dimensions. Not a codec limit -- it's a
# guard against a planner hallucinating something like 100000, which would
# otherwise be accepted and then spend minutes of CPU before failing.
_MAX_DIMENSION = 7680  # 8K wide


async def _run(
    message: StageMessage, previous_output: dict[str, Any] | None, filters: list[str]
) -> dict[str, Any]:
    """Shared tail of every transform: encode, then forward the assets."""
    produced = await process_video(message, previous_output, video_filters=filters)
    return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _even(value: int) -> int:
    """Round down to the nearest even number, floored at 2."""
    return max(2, value - (value % 2))


def _dimension(name: str, raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidMediaParamsError(f"resize's '{name}' must be a whole number of pixels; got {raw!r}")
    if not 1 <= raw <= _MAX_DIMENSION:
        raise InvalidMediaParamsError(
            f"resize's '{name}' must be between 1 and {_MAX_DIMENSION} pixels; got {raw}"
        )
    return _even(raw)


def _aspect_ratio(stage: str, raw: Any) -> float:
    if not isinstance(raw, str) or ":" not in raw:
        raise InvalidMediaParamsError(f"{stage} needs an aspect_ratio like '9:16'; got {raw!r}")
    width, _, height = raw.partition(":")
    try:
        numerator, denominator = float(width), float(height)
    except ValueError:
        raise InvalidMediaParamsError(
            f"{stage}'s aspect_ratio must be two numbers separated by ':'; got {raw!r}"
        ) from None
    if numerator <= 0 or denominator <= 0:
        raise InvalidMediaParamsError(
            f"{stage}'s aspect_ratio must be positive on both sides; got {raw!r}"
        )
    return numerator / denominator


class ResizeWorker(Worker):
    """Scale to explicit pixel dimensions."""

    name = "resize"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - {"width", "height"}
        if unknown:
            raise InvalidMediaParamsError(
                f"resize got unknown parameter(s): {', '.join(sorted(unknown))}"
            )

        width = _dimension("width", params["width"]) if "width" in params else None
        height = _dimension("height", params["height"]) if "height" in params else None
        if width is None and height is None:
            raise InvalidMediaParamsError("resize needs at least a width or a height")

        # -2 means "whatever preserves the aspect ratio, rounded to even",
        # so giving only one dimension scales proportionally rather than
        # squashing the picture.
        return await _run(
            message, previous_output, [f"scale={width or -2}:{height or -2}"]
        )


class RotateWorker(Worker):
    """Rotate by a quarter turn. Arbitrary angles are deliberately not
    offered: they require padding the frame with filler, which is a
    different operation from what anyone means by "rotate this"."""

    name = "rotate"

    # transpose=1 is 90 clockwise, transpose=2 is 90 counter-clockwise.
    # 180 is expressed as hflip+vflip rather than two transposes -- same
    # result, and it avoids swapping the dimensions twice for nothing.
    _FILTERS = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        raw = stage_params(message).get("degrees")
        if isinstance(raw, bool) or raw not in self._FILTERS:
            raise InvalidMediaParamsError(
                f"rotate's 'degrees' must be one of 90, 180, 270; got {raw!r}"
            )
        return await _run(message, previous_output, list(self._FILTERS[raw]))


class FlipWorker(Worker):
    """Mirror the picture. 'horizontal' is the common one -- it un-mirrors
    selfie-camera footage."""

    name = "flip"

    _FILTERS = {"horizontal": "hflip", "vertical": "vflip"}

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        raw = stage_params(message).get("direction")
        if not isinstance(raw, str) or raw.lower() not in self._FILTERS:
            raise InvalidMediaParamsError(
                f"flip's 'direction' must be 'horizontal' or 'vertical'; got {raw!r}"
            )
        return await _run(message, previous_output, [self._FILTERS[raw.lower()]])


class PadWorker(Worker):
    """Letterbox to a target aspect ratio, keeping the whole picture.

    The counterpart to crop: same goal (hit a target ratio), opposite
    trade-off. crop fills the frame and loses the edges; pad keeps
    everything and adds bars.
    """

    name = "pad"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - {"aspect_ratio", "pad_color"}
        if unknown:
            raise InvalidMediaParamsError(
                f"pad got unknown parameter(s): {', '.join(sorted(unknown))}"
            )

        ratio = _aspect_ratio("pad", params.get("aspect_ratio"))
        color = params.get("pad_color", "black")
        if not isinstance(color, str) or not color or not _is_safe_color(color):
            raise InvalidMediaParamsError(
                f"pad's 'pad_color' must be a colour name or #RRGGBB; got {color!r}"
            )

        return await _run(message, previous_output, [_pad_filter(ratio, color)])


def _is_safe_color(color: str) -> bool:
    """Colours go into a filtergraph string, so anything containing the
    characters that delimit one is refused rather than escaped."""
    return all(character.isalnum() or character in "#@" for character in color)


def _pad_filter(ratio: float, color: str) -> str:
    """Expand the canvas to `ratio` and centre the picture in it.

    max() is the mirror of crop's min(): crop shrinks the rect until it
    fits inside the frame, pad grows the canvas until the frame fits
    inside it -- so whichever dimension is already correct stays put and
    the other gains bars. ow/oh are the pad filter's own names for the
    output size it just computed, so the offsets stay expressions and
    nothing needs probing.
    """
    width = rf"ceil(max(iw\,ih*{ratio})/2)*2"
    height = rf"ceil(max(ih\,iw/{ratio})/2)*2"
    return f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:{color}"
