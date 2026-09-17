"""CropWorker — reframe a video to a target aspect ratio (Phase 5B).

The first real editing capability, and the template the rest of 5B follows:
parse this stage's params, turn them into an ffmpeg filter, hand that to
media.process_video. Storage, temp files, subprocess handling, asset
chaining and preview mode all live there, which is why this file is short.

Crops rather than letterboxes: reframing 16:9 footage to 9:16 for Reels or
Shorts is the single most common real editing request, and users asking for
it want the frame filled, not black bars. `pad` is the sibling capability
for the letterbox case.
"""

from typing import Any

from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    Asset,
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    previous_assets,
    process_video,
    stage_params,
)


class CropWorker(Worker):
    name = "crop"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        ratio = _parse_aspect_ratio(stage_params(message).get("aspect_ratio"))
        produced = await process_video(
            message, previous_output, video_filters=[_crop_filter(ratio)]
        )
        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _parse_aspect_ratio(raw: Any) -> float:
    """"9:16" -> 0.5625.

    Every rejection here is an InvalidMediaParamsError (a PermanentError):
    the planner produced a value this capability can't use, and it will be
    exactly as unusable on redelivery, so there is nothing to gain from
    spending the retry budget on it.
    """
    if not isinstance(raw, str) or ":" not in raw:
        raise InvalidMediaParamsError(
            f"crop needs an aspect_ratio like '9:16'; got {raw!r}"
        )

    width, _, height = raw.partition(":")
    try:
        numerator, denominator = float(width), float(height)
    except ValueError:
        raise InvalidMediaParamsError(
            f"crop's aspect_ratio must be two numbers separated by ':'; got {raw!r}"
        ) from None

    if numerator <= 0 or denominator <= 0:
        raise InvalidMediaParamsError(
            f"crop's aspect_ratio must be positive on both sides; got {raw!r}"
        )
    return numerator / denominator


def _crop_filter(ratio: float) -> str:
    """A centred crop to `ratio`, expressed in ffmpeg's own filter language.

    Computed by ffmpeg from the input's dimensions (iw/ih) rather than by
    probing first: a probe would mean downloading the video twice, once to
    measure and once to edit. min() keeps the rect inside the frame
    whichever way the source is oriented, so this same expression widens or
    heightens correctly without branching here.

    floor(.../2)*2 forces even dimensions -- h264 with yuv420p chroma
    subsampling cannot encode odd ones, and ffmpeg fails outright rather
    than rounding for you. It costs at most a pixel against the exact
    ratio.

    Commas inside min() are escaped because a bare comma separates filters
    in a filtergraph. x/y are omitted, which ffmpeg reads as centred.
    """
    return (
        rf"crop=floor(min(iw\,ih*{ratio})/2)*2:floor(min(ih\,iw/{ratio})/2)*2"
    )
