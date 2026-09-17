"""RenderWorker — produce the final deliverable (Phase 5G).

A normal capability, not a special "final" stage. WorkflowEngine already
treats the last stage generically, so nothing here needs to know it is
last — and treating it specially would make `crop -> render -> trim` an
error rather than the perfectly reasonable "export a master, then cut a
teaser from it" that it is.

Its Result.artifact_uri is what a client's download link resolves to, via
GET /jobs/{id}/artifacts.
"""

from typing import Any

from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    AssetKind,
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    is_preview,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    process_video,
    put_asset,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
)

# Codec pairs per container. Chosen for playability rather than novelty:
# h264/aac plays everywhere, vp9/opus is what webm exists for.
_FORMATS: dict[str, dict[str, Any]] = {
    "mp4": {"suffix": ".mp4", "video": "libx264", "audio": "aac"},
    "mov": {"suffix": ".mov", "video": "libx264", "audio": "aac"},
    "webm": {"suffix": ".webm", "video": "libvpx-vp9", "audio": "libopus"},
    "gif": {"suffix": ".gif", "video": None, "audio": None},
}

# Shorthands people actually say. Height-based, with width derived, so
# they mean the same thing for vertical footage as for landscape.
_RESOLUTIONS = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

_GIF_FPS = 12
_PREVIEW_HEIGHT = 480


class RenderWorker(Worker):
    name = "render"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - {"resolution", "format", "bitrate"}
        if unknown:
            raise InvalidMediaParamsError(
                f"render got unknown parameter(s): {', '.join(sorted(unknown))}"
            )

        container = _parse_format(params.get("format", "mp4"))
        height = _parse_resolution(params.get("resolution"))
        bitrate = _parse_bitrate(params.get("bitrate"))

        # A preview of a workflow ending in render should still be cheap,
        # so the requested resolution is capped rather than honoured.
        if is_preview(message):
            height = min(height or _PREVIEW_HEIGHT, _PREVIEW_HEIGHT)

        if container == "gif":
            produced = await _render_gif(message, previous_output, height)
        else:
            produced = await _render_video(
                message, previous_output, container, height, bitrate
            )
        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


def _parse_format(raw: Any) -> str:
    if not isinstance(raw, str) or raw.lower().lstrip(".") not in _FORMATS:
        raise InvalidMediaParamsError(
            f"render's 'format' must be one of {', '.join(sorted(_FORMATS))}; got {raw!r}"
        )
    return raw.lower().lstrip(".")


def _parse_resolution(raw: Any) -> int | None:
    """Target height, or None to keep the source's.

    Accepts both the shorthand people say out loud ("1080p") and an
    explicit "1920x1080"; in the latter case only the height is used,
    because width is derived to preserve the aspect ratio -- forcing both
    is what `resize` is for, and doing it silently here would stretch the
    picture at the very last step.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise InvalidMediaParamsError(
            f"render's 'resolution' must be like '1080p' or '1920x1080'; got {raw!r}"
        )

    value = raw.strip().lower()
    if value in _RESOLUTIONS:
        return _RESOLUTIONS[value]
    if "x" in value:
        width, _, height = value.partition("x")
        # Both halves must be present. The width is then discarded (height
        # drives the scale, width follows the aspect ratio) -- but a value
        # missing one half is a malformed request, not a terser way of
        # saying the same thing, and accepting it would suggest the width
        # had been honoured when it never is.
        if width.isdigit() and height.isdigit():
            parsed = int(height)
            if 16 <= parsed <= 4320:
                return parsed - (parsed % 2)
    raise InvalidMediaParamsError(
        f"render's 'resolution' must be like '1080p' or '1920x1080'; got {raw!r}"
    )


def _parse_bitrate(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise InvalidMediaParamsError(
            f"render's 'bitrate' must be like '5M' or '2500k'; got {raw!r}"
        )
    value = str(raw).strip().lower()
    if value.isdigit():
        # A bare number is kilobits per second, which is how bitrate is
        # quoted everywhere it appears in a UI.
        return f"{int(value)}k"
    if value[:-1].replace(".", "", 1).isdigit() and value[-1] in "km":
        return value
    raise InvalidMediaParamsError(
        f"render's 'bitrate' must be like '5M' or '2500k'; got {raw!r}"
    )


async def _render_video(
    message: StageMessage,
    previous_output: dict[str, Any] | None,
    container: str,
    height: int | None,
    bitrate: str | None,
) -> Any:
    spec = _FORMATS[container]
    filters = [f"scale=-2:{height}"] if height else []
    # -2 keeps width even for the chroma subsampling h264 and vp9 both use.

    output_args = ["-c:v", spec["video"], "-c:a", spec["audio"]]
    if bitrate:
        output_args += ["-b:v", bitrate]
    if container in ("mp4", "mov"):
        # +faststart moves the index to the front so the file starts
        # playing before it has fully downloaded -- the difference between
        # a link that plays and one that appears broken until it finishes.
        output_args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]

    return await process_video(
        message,
        previous_output,
        video_filters=filters,
        output_args=output_args,
        suffix=spec["suffix"],
    )


async def _render_gif(
    message: StageMessage, previous_output: dict[str, Any] | None, height: int | None
) -> Any:
    """GIF gets its own path: it carries no audio, and needs a per-clip
    palette to avoid the muddy banding a default 256-colour conversion
    produces. palettegen/paletteuse is a branching filtergraph, which
    process_video's comma-joined filter list cannot express.
    """
    scale = f"scale=-2:{height or 360}:flags=lanczos"
    filtergraph = (
        f"fps={_GIF_FPS},{scale},split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
    )

    source = resolve_input_uri(message, previous_output)
    with materialize_to_tempfile(source) as path:
        with output_tempfile(".gif") as destination:
            await run_ffmpeg(["-i", str(path), "-vf", filtergraph, "-an", str(destination)])
            if not destination.is_file() or destination.stat().st_size == 0:
                raise InvalidMediaParamsError("render produced no output")
            return put_asset(destination, kind=AssetKind.VIDEO)
