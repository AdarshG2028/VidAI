"""find_content -- semantic frame matching: where on screen is the thing
the user described?

**This is frame matching, not object tracking.** Frames are sampled at an
interval, each is judged independently, and hits are coalesced into ranges.
A subject who leaves and re-enters within one interval reads as
continuously present. That is a deliberate accuracy ceiling of the
approach, not a defect -- the capability description must not promise more,
and the ceiling is what keeps cost bounded.

**Cost is O(1) in video length, on purpose.** The worker samples at
max(interval, duration / max_frames), so the frame cap binds on long
videos: a 3-hour upload costs the same as a 10-minute one, just sampled
more coarsely. Without the cap a single long upload is an unbounded bill,
since every frame is billed tokens.

**Measured, not estimated** (Groq, qwen/qwen3.6-27b, free tier):

* An image *bills* ~845-922 prompt tokens, flat, at every frame width
  tried from 192px to 512px. Frame resolution is a quality knob, not a
  cost one.
* Rate limiting is accounted separately and far more expensively: ~2,786
  tokens per image against the tier's tokens-per-minute budget. At 8,000
  TPM that is **under three frames a minute**, whatever the concurrency --
  which is why vision_request_concurrency defaults to 1 and the client
  paces itself rather than failing (see GroqVisionClient).
* So `vision_max_frames` is the dial that matters, and at its default of
  150 a single search needs ~52 minutes of free-tier budget. A paid tier
  is what makes this feature interactive; capping frames is what makes it
  affordable. Both are config, not code.

**Timestamps come from ffmpeg, never from arithmetic on the model's
output.** The model is asked only *which frame ids* matched; the times
those ids correspond to are parsed from ffmpeg's own showinfo output. A
model that returns fewer verdicts than images sent is a realistic failure,
and positional alignment would silently shift every later timestamp and
cut the wrong footage.

**Not cached.** video_assets is UNIQUE(video_id, kind) with an upserting
write, so two different queries against one video would overwrite each
other's results. detect_scenes sets cache_key=None whenever a threshold is
given for the same reason; here a query is always given, so the opt-out
would be unconditional and the cache is simply not wired in.
"""

import asyncio
import json
import logging
import re
from typing import Any

from backend.core.config import get_settings
from backend.services.vision_client import (
    GroqVisionClient,
    UnusableFramesError,
    VisionClient,
    VisionError,
)
from backend.workers.base import StageMessage, Worker
from backend.workers.content_ranges import coalesce
from backend.workers.media import (
    Asset,
    AssetKind,
    InvalidMediaParamsError,
    MediaProcessingError,
    assets_payload,
    forward_assets,
    materialize_to_tempfile,
    output_tempfile,
    previous_assets,
    probe,
    put_asset_bytes,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
)

logger = logging.getLogger(__name__)

# Same regex scene detection uses against showinfo's stderr.
_PTS_TIME_RE = re.compile(r"pts_time:([\d.]+)")

# Width frames are downscaled to before being sent. Comfortably enough for
# a VLM to read a shirt colour or recognise an object, and it keeps a
# 5-frame batch at a few hundred KB against the provider's 20MB ceiling.
_FRAME_WIDTH = 512

# A query longer than this is not a description of something on screen.
# Also a cheap guard on prompt size, since the query is interpolated into
# every batch request.
_MAX_QUERY_CHARS = 300


class ContentSearchWorker(Worker):
    name = "find_content"

    def __init__(self, client: VisionClient | None = None) -> None:
        # Injectable so tests exercise real frame extraction and range
        # coalescing without calling a paid API -- same shape as
        # TranscribeWorker's TranscriptionClient.
        self._client = client

    def _resolve_client(self) -> VisionClient:
        if self._client is not None:
            return self._client
        settings = get_settings()
        if not settings.groq_api_key:
            raise MediaProcessingError(
                "find_content needs GROQ_API_KEY; the worker cannot look at the "
                "video without it"
            )
        return GroqVisionClient(
            api_key=settings.groq_api_key,
            model=settings.groq_vision_model,
            reasoning_effort=settings.vision_reasoning_effort,
            max_tokens=settings.vision_max_output_tokens,
            rate_limit_retries=settings.vision_rate_limit_retries,
        )

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        query = _parse_query(stage_params(message))
        settings = get_settings()
        carried = previous_assets(previous_output)
        source_uri = resolve_input_uri(message, previous_output)

        with materialize_to_tempfile(source_uri) as source:
            probe_data = await probe(source)
            duration = max(0.0, float(probe_data.get("format", {}).get("duration") or 0.0))
            if duration <= 0:
                raise InvalidMediaParamsError(
                    "find_content could not determine how long this video is"
                )

            interval = _sample_interval(duration, settings)
            frames, times = await _sample_frames(
                source, interval=interval, max_frames=settings.vision_max_frames
            )

        if not frames:
            raise InvalidMediaParamsError(
                "find_content could not read any frames from this video"
            )

        hit_times = await self._inspect(frames, times, query=query, settings=settings)
        matches = coalesce(
            hit_times,
            interval=interval,
            duration=duration,
            # One missed sample, not two. After widening, consecutive hits
            # abut (gap 0) and a single skipped frame leaves a gap of
            # exactly `interval` -- so this bridges a brief occlusion and
            # nothing more. 2*interval was tried first and was wrong: it
            # also bridges two consecutive misses, which merged three
            # genuinely separate appearances into one range and made
            # remove_matches cut footage the subject was never in. That
            # direction contradicts the deliberate bias toward keeping too
            # much rather than too little.
            bridge_gap=interval,
            min_range=0.5,
            pad=0.25,
        )

        payload = {
            "query": query,
            "sample_interval_seconds": round(interval, 3),
            "frames_examined": len(frames),
            "matches": [{"start": round(s, 3), "end": round(e, 3)} for s, e in matches],
            "matched_seconds": round(sum(e - s for s, e in matches), 3),
            "duration_seconds": round(duration, 3),
        }
        logger.info(
            "content search completed",
            extra={
                "query": query,
                "frames_examined": len(frames),
                "hits": len(hit_times),
                "ranges": len(matches),
            },
        )

        # An empty match list is a successful search that found nothing --
        # the same call detect_scenes makes for a single continuous shot.
        # The error belongs to remove_matches/keep_matches, which know the
        # polarity and can say what that means.
        produced = put_asset_bytes(
            json.dumps(payload, indent=2).encode("utf-8"),
            "content_matches.json",
            AssetKind.CONTENT_MATCHES,
        )
        return _finish(carried, source_uri, produced)

    async def _inspect(
        self,
        frames: list[bytes],
        times: list[float],
        *,
        query: str,
        settings: Any,
    ) -> list[float]:
        """Frame timestamps the model reported the subject visible in.

        Batched to the provider's per-request image limit and run through a
        semaphore: fully sequential is a latency problem for a room watching
        the job, fully parallel invites rate limiting.
        """
        client = self._resolve_client()
        batch_size = max(1, settings.vision_frames_per_request)
        semaphore = asyncio.Semaphore(max(1, settings.vision_request_concurrency))

        # Ids are positional but never *relied on* positionally -- they are
        # sent to the model and matched back by name, so a response that
        # omits or reorders entries cannot shift a timestamp.
        batches = [
            [(f"f{i}", frames[i]) for i in range(start, min(start + batch_size, len(frames)))]
            for start in range(0, len(frames), batch_size)
        ]

        async def run(batch: list[tuple[str, bytes]]):
            async with semaphore:
                try:
                    return await client.inspect_frames(batch, query=query)
                except UnusableFramesError as exc:
                    # The frames themselves are rejected; identical bytes
                    # fail identically, so retrying spends the budget for
                    # nothing.
                    raise InvalidMediaParamsError(
                        f"the vision provider rejected these frames: {exc}"
                    ) from exc
                except VisionError as exc:
                    # A failed batch fails the whole stage. Treating it as
                    # "no matches in those frames" would let the coalescer
                    # bridge the hole and emit ranges that are confidently,
                    # invisibly wrong.
                    raise MediaProcessingError(
                        f"the vision provider failed while looking at this video: {exc}"
                    ) from exc

        results = await asyncio.gather(*(run(batch) for batch in batches))

        hits: list[float] = []
        reported: set[str] = set()
        for verdicts in results:
            for verdict in verdicts:
                reported.add(verdict.frame_id)
                if not verdict.visible or verdict.confidence < settings.vision_min_confidence:
                    continue
                index = _frame_index(verdict.frame_id)
                if index is not None and index < len(times):
                    hits.append(times[index])

        missing = len(frames) - len(reported)
        if missing > 0:
            # Not fatal: an unreported frame counts as not-visible, which is
            # the safe direction for a search that drives a cut. Logged
            # because a provider that routinely under-reports would quietly
            # shrink every result.
            logger.warning(
                "vision provider did not report on every frame",
                extra={"frames_sent": len(frames), "frames_missing": missing},
            )
        return hits


def _parse_query(params: dict[str, Any]) -> str:
    unknown = set(params) - {"query"}
    if unknown:
        raise InvalidMediaParamsError(
            f"find_content got unknown parameter(s): {', '.join(sorted(unknown))}"
        )
    query = params.get("query")
    # Checked here because the proposal validator has no required-params
    # concept -- it only type-checks params that were actually supplied, so
    # an omitted query reaches the worker.
    if not isinstance(query, str) or not query.strip():
        raise InvalidMediaParamsError(
            "find_content needs a 'query' describing what to look for, "
            "e.g. 'the man in the red shirt'"
        )
    if len(query) > _MAX_QUERY_CHARS:
        raise InvalidMediaParamsError(
            f"find_content's 'query' must be under {_MAX_QUERY_CHARS} characters; "
            "it should describe what is on screen, not a long instruction"
        )
    return query.strip()


def _sample_interval(duration: float, settings: Any) -> float:
    """Seconds between sampled frames, bounded at both ends.

    Two bounds, for opposite failure modes:

    * `vision_max_frames` is a **floor** on the interval -- a 3-hour upload
      samples every 72s rather than costing 5,400 frames.
    * `vision_min_frames` is a **ceiling** on it -- a 12s clip samples
      every 0.5s rather than six times total.

    The ceiling is the one that is easy to omit, and omitting it was a real
    bug rather than a hypothetical: at a flat 2s interval, three separate
    one-second appearances in a 12s clip were sampled once each, and the
    coalescer -- correctly, for that interval -- bridged the one-sample
    gaps between them into a single 9-second range. remove_matches then cut
    6 seconds the subject was never in. Sampling finer resolves them into
    three ranges without touching the coalescer, whose bridging still has
    to hold for genuine occlusions.

    They cannot fight: min_frames < max_frames, so duration/max_frames is
    always the smaller of the two, and the floor is applied last.
    """
    floor = max(
        settings.vision_sample_interval_seconds,
        duration / max(1, settings.vision_max_frames),
    )
    ceiling = duration / max(1, settings.vision_min_frames)
    return max(min(floor, ceiling), settings.vision_min_sample_interval_seconds)


async def _sample_frames(
    source, *, interval: float, max_frames: int
) -> tuple[list[bytes], list[float]]:
    """JPEG bytes and their real timestamps, one ffmpeg pass.

    Timestamps come from showinfo rather than `index * interval`: the fps
    filter rounds, a source may be variable-frame-rate, and the first frame
    is not always at t=0. Falls back to the nominal arithmetic (with a
    warning) only if the two counts disagree, so a surprising ffmpeg build
    degrades instead of crashing.

    Frames are never stored as assets -- they are read, sent, and discarded
    with the temp directory. Nothing references them afterwards, so there is
    nothing for the artifact sweeper to clean up.
    """
    with output_tempfile(".jpg") as stub:
        pattern = stub.parent / "frame_%04d.jpg"
        stderr = await run_ffmpeg(
            [
                "-i",
                str(source),
                "-vf",
                f"fps=1/{interval},scale={_FRAME_WIDTH}:-2,showinfo",
                "-frames:v",
                str(max_frames),
                "-q:v",
                "5",
                str(pattern),
            ]
        )
        paths = sorted(stub.parent.glob("frame_*.jpg"))
        frames = [path.read_bytes() for path in paths]

    times = [float(t) for t in _PTS_TIME_RE.findall(stderr)]
    if len(times) != len(frames):
        logger.warning(
            "showinfo timestamps did not match extracted frame count; "
            "falling back to nominal sample times",
            extra={"frames": len(frames), "timestamps": len(times)},
        )
        times = [i * interval for i in range(len(frames))]
    return frames, times


def _frame_index(frame_id: str) -> int | None:
    try:
        return int(frame_id[1:]) if frame_id.startswith("f") else None
    except ValueError:
        return None


def _finish(carried: list[Asset], source_uri: str, matches: Asset) -> dict[str, Any]:
    """Forward the video alongside the matches.

    The insert matters and is easy to miss: find_content is normally stage
    0, where nothing was carried. Without it, `[find_content,
    remove_matches]` hands the next stage a payload with no video asset --
    and since stage 1 has no compiled video_uris either, it dies in
    resolve_input_uri with "no input video". detect_filler_words omits this
    only because a transcript always precedes it, so it can never be first.
    """
    produced = [matches]
    if not carried:
        produced.insert(0, Asset(kind=AssetKind.VIDEO, uri=source_uri))
    return assets_payload(forward_assets(carried, produced))
