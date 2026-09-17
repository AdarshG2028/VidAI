"""SubtitleBurnWorker — render captions permanently into the picture (5F).

Consumes the `srt` asset `transcribe` produced, wherever in the workflow
that happened: monotonic accumulation (media.forward_assets) carries it
past any number of unrelated stages, so `transcribe -> color -> crop ->
burn_subtitles` works without the two ever being adjacent.

A proposal that asks to burn subtitles with nothing producing an srt is
rejected at *validation* time by the capability's requires_asset_kinds
(Phase 5A, Step 5), so reaching this worker without one means the job was
submitted directly through the jobs API, bypassing the planner.
"""

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
    process_video,
    put_asset,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
)

# Deliberately conservative: white text, black outline, near the bottom.
# It is legible over any footage, which a coloured or unbordered style is
# not, and it matches what viewers already expect captions to look like.
_DEFAULT_STYLE = "FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=30"

_STYLES = {
    "default": _DEFAULT_STYLE,
    "large": "FontSize=34,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=3,MarginV=40",
    "small": "FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=20",
}


class SubtitleBurnWorker(Worker):
    name = "burn_subtitles"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - {"style"}
        if unknown:
            raise InvalidMediaParamsError(
                f"burn_subtitles got unknown parameter(s): {', '.join(sorted(unknown))}"
            )
        style_name = params.get("style", "default")
        if style_name not in _STYLES:
            raise InvalidMediaParamsError(
                f"burn_subtitles' 'style' must be one of {', '.join(sorted(_STYLES))}; "
                f"got {style_name!r}"
            )

        carried = previous_assets(previous_output)
        subtitle = next((a for a in carried if a.kind == AssetKind.SRT), None)
        if subtitle is None:
            raise InvalidMediaParamsError(
                "burn_subtitles needs a subtitle track, but no stage before it "
                "produced one — add a 'transcribe' stage earlier in the workflow"
            )

        source_uri = resolve_input_uri(message, previous_output)

        with materialize_to_tempfile(subtitle.uri, suffix=".srt") as srt_path:
            with materialize_to_tempfile(source_uri) as video_path:
                with output_tempfile(".mp4") as destination:
                    # ffmpeg runs from the subtitle's own directory so the
                    # filter can name it bare. Its path would otherwise be
                    # parsed by the filtergraph lexer, where backslashes
                    # escape and colons separate options -- a Windows path
                    # is destroyed by that (see run_ffmpeg's `cwd`).
                    await run_ffmpeg(
                        [
                            "-i", str(video_path),
                            "-vf",
                            f"subtitles={srt_path.name}:force_style='{_STYLES[style_name]}'",
                            "-c:a", "copy",
                            str(destination),
                        ],
                        cwd=srt_path.parent,
                    )
                    if not destination.is_file() or destination.stat().st_size == 0:
                        raise InvalidMediaParamsError(
                            "burn_subtitles produced no output"
                        )
                    produced = put_asset(destination, kind=AssetKind.VIDEO)

        return assets_payload(forward_assets(carried, [produced]))
