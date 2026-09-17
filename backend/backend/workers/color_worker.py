"""ColorWorker — visual adjustments (Phase 5C).

One capability covering brightness, contrast, saturation, gamma and
sharpen, rather than five as in 5B's spatial transforms. The difference is
real, not stylistic: these all compile into a single ffmpeg filtergraph
and are order-insensitive between themselves, so nothing is gained by
letting the planner sequence them -- while crop-then-scale genuinely
differs from scale-then-crop, which is why those stayed separate.

It also matches how people ask: "brighter and more punchy" is one request,
not three.
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

# Ranges are ffmpeg's own for the eq filter, narrowed to what is
# defensible on real footage: eq accepts contrast up to 1000, but anything
# past ~3 is a solarised mess rather than an edit anyone asked for.
# (minimum, maximum, neutral value)
_LIMITS: dict[str, tuple[float, float, float]] = {
    "brightness": (-1.0, 1.0, 0.0),
    "contrast": (0.0, 3.0, 1.0),
    "saturation": (0.0, 3.0, 1.0),
    "gamma": (0.1, 10.0, 1.0),
    "sharpen": (0.0, 2.0, 0.0),
}


class ColorWorker(Worker):
    name = "color"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        filters = _build_filters(stage_params(message))
        produced = await process_video(message, previous_output, video_filters=filters)
        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _coerce(name: str, raw: Any) -> float:
    """Validate one adjustment, or raise permanently.

    int is accepted alongside float for the same reason validate_proposal
    accepts it: JSON has one number type, and a planner writes `1` as
    readily as `1.0`. bool is excluded -- it is an int subclass in Python,
    and "saturation": true is not an adjustment.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InvalidMediaParamsError(
            f"color's '{name}' must be a number; got {raw!r}"
        )

    low, high, _ = _LIMITS[name]
    value = float(raw)
    if not low <= value <= high:
        raise InvalidMediaParamsError(
            f"color's '{name}' must be between {low} and {high}; got {value}"
        )
    return value


def _build_filters(params: dict[str, Any]) -> list[str]:
    """Turn the requested adjustments into an ffmpeg filtergraph.

    Only the adjustments actually asked for are emitted, so an unmentioned
    channel is left genuinely untouched rather than re-stated at its
    neutral value.
    """
    unknown = set(params) - set(_LIMITS)
    if unknown:
        # validate_proposal already rejects unknown params at proposal
        # time; this covers a job submitted directly through the jobs API,
        # which bypasses that path entirely.
        raise InvalidMediaParamsError(
            f"color got unknown parameter(s): {', '.join(sorted(unknown))}"
        )

    values = {name: _coerce(name, params[name]) for name in params}
    if not values:
        raise InvalidMediaParamsError(
            "color needs at least one adjustment "
            f"({', '.join(sorted(_LIMITS))}); got none"
        )

    # A value equal to the neutral point is a no-op, so a request made
    # entirely of them would re-encode the video to change nothing.
    if all(value == _LIMITS[name][2] for name, value in values.items()):
        raise InvalidMediaParamsError(
            f"color was asked for no actual change: {values}"
        )

    filters: list[str] = []
    eq_terms = [
        f"{name}={values[name]}"
        for name in ("brightness", "contrast", "saturation", "gamma")
        if name in values
    ]
    if eq_terms:
        filters.append("eq=" + ":".join(eq_terms))

    if "sharpen" in values:
        # unsharp's 5:5 luma matrix is its documented default; only the
        # amount is exposed, since matrix size is a detail no planner
        # (and few users) would have an opinion about.
        filters.append(f"unsharp=5:5:{values['sharpen']}")
    return filters
