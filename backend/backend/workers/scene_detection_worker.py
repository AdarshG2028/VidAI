"""SceneDetectionWorker — shot/scene-cut timestamps (Phase 10).

Detects cuts with ffmpeg's own scene-change filter
(`select='gt(scene,X)'` + `showinfo`), not a new dependency: every other
capability in this project already runs its video work through ffmpeg
(backend/workers/media.py), and pulling in a scene-detection library
would repeat the exact tradeoff Phase 5F deliberately avoided when it
chose Groq-hosted Whisper over local faster-whisper specifically to keep
the deployment small (8GB -> 4GB, see ec2-deployment-reference).

Produces a single new "scenes" asset (JSON: cut timestamps) and passes
the video through untouched -- this stage never re-encodes, same shape
as transcribe.
"""

import json
import logging
import re
from typing import Any

from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    materialize_to_tempfile,
    previous_assets,
    primary_video,
    put_asset_bytes,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
    stage_video_ids,
)
from backend.workers.video_asset_cache import SqlVideoAssetCache, VideoAssetCache

logger = logging.getLogger(__name__)

# ffmpeg's own suggested starting point for its scene filter. Higher is
# stricter (fewer, more confident cuts); lower catches subtler cuts at
# the cost of false positives on fast pans/flashes.
_DEFAULT_THRESHOLD = 0.4
_PTS_TIME_RE = re.compile(r"pts_time:([\d.]+)")


class SceneDetectionWorker(Worker):
    name = "detect_scenes"

    def __init__(self, cache: VideoAssetCache | None = None) -> None:
        # Same injected-cache shape as TranscribeWorker, and literally the
        # same class by default -- VideoAssetCache was generalized off of
        # transcribe's cache specifically so this wouldn't need its own.
        self._cache = cache if cache is not None else SqlVideoAssetCache()

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - {"threshold"}
        if unknown:
            raise InvalidMediaParamsError(
                f"detect_scenes got unknown parameter(s): {', '.join(sorted(unknown))}"
            )
        threshold_param = params.get("threshold")
        if threshold_param is not None:
            if not isinstance(threshold_param, (int, float)) or isinstance(threshold_param, bool):
                raise InvalidMediaParamsError(
                    "detect_scenes's 'threshold' must be a number between 0 and 1 "
                    f"(exclusive of 0); got {threshold_param!r}"
                )
            if not (0.0 < threshold_param <= 1.0):
                raise InvalidMediaParamsError(
                    "detect_scenes's 'threshold' must be between 0 and 1 (exclusive "
                    f"of 0); got {threshold_param!r}"
                )
        threshold = float(threshold_param) if threshold_param is not None else _DEFAULT_THRESHOLD

        source_uri = resolve_input_uri(message, previous_output)
        carried = previous_assets(previous_output)

        # Same cacheability gate as TranscribeWorker, same reasoning: only
        # valid when reading the pristine upload (nothing upstream
        # re-encoded it yet), and an explicit threshold override means a
        # different result, so it opts out of the shared cache entry
        # rather than serving or storing a mismatched one.
        cache_key = None
        if threshold_param is None and primary_video(carried) is None:
            ids = stage_video_ids(message)
            if ids and ids[0]:
                cache_key = ids[0]

        if cache_key is not None:
            try:
                cached = await self._cache.get(cache_key, AssetKind.SCENES)
            except Exception:
                logger.warning(
                    "failed to read cached scenes for video %s", cache_key, exc_info=True
                )
                cached = None
            if cached is not None:
                return _finish(carried, source_uri, cached)

        # No cwd trick needed here (contrast subtitle_burn_worker's
        # `subtitles=` filter): select/showinfo take no file-path
        # argument, so there's nothing for the filtergraph lexer to
        # mangle -- the absolute path goes straight to -i, same as
        # transcribe's own _extract_audio.
        with materialize_to_tempfile(source_uri) as path:
            stderr = await run_ffmpeg(
                [
                    "-i",
                    str(path),
                    "-vf",
                    f"select='gt(scene,{threshold})',showinfo",
                    "-f",
                    "null",
                    "-",
                ]
            )

        # A single continuous shot has no cuts -- an empty list is the
        # correct result, not an error; nothing here raises on it.
        cuts = [{"time": float(t)} for t in _PTS_TIME_RE.findall(stderr)]

        scenes = put_asset_bytes(
            json.dumps({"threshold": threshold, "cuts": cuts}, indent=2).encode("utf-8"),
            "scenes.json",
            AssetKind.SCENES,
        )

        if cache_key is not None:
            try:
                await self._cache.put(cache_key, AssetKind.SCENES, scenes)
            except Exception:
                logger.warning(
                    "failed to cache scenes for video %s", cache_key, exc_info=True
                )

        return _finish(carried, source_uri, scenes)


def _finish(carried: list[Asset], source_uri: str, scenes: Asset) -> dict[str, Any]:
    produced = [scenes]
    if not carried:
        produced.insert(0, Asset(kind=AssetKind.VIDEO, uri=source_uri))
    return assets_payload(forward_assets(carried, produced))
