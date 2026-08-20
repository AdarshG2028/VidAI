"""remove_matches / keep_matches -- the two consumers of find_content's
`content_matches` asset, and the N-segment filtergraph they share.

**Two capabilities, not one with a `mode` param.** The proposal validator
has no required-params concept (it iterates only the params a proposal
actually supplies), so a `mode` the planner omitted or misspelled would
pass validation and invert the cut -- producing a plausible-looking video
containing exactly the wrong footage, with nothing in the proposal the room
votes on to show it. `trim` vs `remove_segment` split for this same reason
after a live incident; polarity belongs in the stage name, not a parameter.
The two workers differ by one boolean.

**Generalises remove_segment_worker, not merge_worker.** The structure is
the same: trim the keep-segments out of one decoded stream and concat them.
merge's canvas/fps normalisation and synthesised silence exist because it
joins *different files*; every segment here comes from one source and
already shares geometry, frame rate and audio presence.

**Explicit split/asplit at K >= 2.** remove_segment reuses `[0:v]` across
two independent chains without one, verified at N=2 -- but K here is
unbounded, so this emits `split` rather than carrying that assumption to
an N it was never checked at. split duplicates references, not frames, so
it costs nothing.
"""

import json
import logging
from abc import abstractmethod
from typing import Any

from backend.core.config import get_settings
from backend.storage import get_storage
from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    PREVIEW_OUTPUT_ARGS,
    PREVIEW_SCALE_FILTER,
    Asset,
    AssetKind,
    InvalidMediaParamsError,
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

logger = logging.getLogger(__name__)

# Matches remove_segment_worker's floor: below this a segment is not a clip
# anyone asked for, and ffmpeg's trim would emit an empty or single-frame
# piece that concat then chokes on.
_MIN_DURATION_SECONDS = 0.1


def build_keep_filtergraph(
    keep: list[tuple[float, float]], *, has_audio: bool, preview: bool
) -> tuple[str, list[str]]:
    """The filter_complex and output maps that keep exactly `keep`.

    Caller guarantees `keep` is sorted, non-overlapping, clamped to the
    source and all segments at least _MIN_DURATION_SECONDS long -- this
    function does no validation, so it stays a pure string builder that can
    be tested without ffmpeg or a video.

    K == 1 emits no concat at all: concat needs two real inputs, and a
    single surviving segment is just a trim.
    """
    k = len(keep)
    if k == 0:
        raise ValueError("build_keep_filtergraph needs at least one segment to keep")

    video_pad = "vraw" if preview else "outv"
    chains: list[str] = []

    if k == 1:
        start, end = keep[0]
        chains.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[{video_pad}]")
        if has_audio:
            chains.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[outa]")
    else:
        # split/asplit first so each trim reads its own copy of the stream.
        chains.append("[0:v]split=" + str(k) + "".join(f"[sv{i}]" for i in range(k)))
        if has_audio:
            chains.append("[0:a]asplit=" + str(k) + "".join(f"[sa{i}]" for i in range(k)))

        for i, (start, end) in enumerate(keep):
            chains.append(
                f"[sv{i}]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]"
            )
            if has_audio:
                chains.append(
                    f"[sa{i}]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
                )

        # Interleaved [v0][a0][v1][a1]... so one concat handles both
        # streams, rather than two parallel concats that could disagree
        # about segment count.
        if has_audio:
            segments = "".join(f"[v{i}][a{i}]" for i in range(k))
            chains.append(f"{segments}concat=n={k}:v=1:a=1[{video_pad}][outa]")
        else:
            segments = "".join(f"[v{i}]" for i in range(k))
            chains.append(f"{segments}concat=n={k}:v=1:a=0[{video_pad}]")

    if preview:
        chains.append(f"[{video_pad}]{PREVIEW_SCALE_FILTER}[outv]")

    outputs = ["-map", "[outv]"]
    if has_audio:
        outputs += ["-map", "[outa]"]
    return ";".join(chains), outputs


def complement(
    ranges: list[tuple[float, float]], *, duration: float
) -> list[tuple[float, float]]:
    """Everything in [0, duration] that `ranges` does not cover.

    Requires sorted, non-overlapping input -- overlapping ranges would
    produce negative-length gaps here, which is why content_ranges.coalesce
    re-merges after padding.
    """
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in ranges:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append((cursor, duration))
    return gaps


def _load_matches(previous_output: dict[str, Any] | None, stage: str) -> dict[str, Any]:
    """The content_matches payload produced by an earlier find_content.

    Read whole rather than streamed to disk: this is a small ranges JSON the
    worker itself parses, which is the convention detect_filler_words
    follows for the transcript. Assets that go to ffmpeg get
    materialize_to_tempfile instead.
    """
    carried = previous_assets(previous_output)
    asset = next((a for a in carried if a.kind == AssetKind.CONTENT_MATCHES), None)
    if asset is None:
        raise InvalidMediaParamsError(
            f"{stage} needs the ranges a content search produced, but no stage "
            "before it produced any -- add a 'find_content' stage earlier in "
            "the workflow, with a description of what to look for"
        )
    try:
        return json.loads(get_storage().get(asset.uri))
    except InvalidMediaParamsError:
        raise
    except Exception as exc:
        raise InvalidMediaParamsError(
            f"{stage} could not read the content search results: {exc}"
        ) from exc


def _clamped_matches(payload: dict[str, Any], duration: float) -> list[tuple[float, float]]:
    """Match ranges, clamped to the video actually being edited.

    Clamping is not paranoia. `[find_content, trim, remove_matches]`
    validates structurally -- assets accumulate monotonically, so the
    ranges survive the trim -- but they were computed against the *pre-trim*
    timeline and no longer describe this video. That workflow is
    discouraged in the capability descriptions and cannot be prevented
    structurally, so the ranges are clamped rather than trusted, and
    anything left too short to cut is dropped.
    """
    raw = payload.get("matches", []) if isinstance(payload, dict) else []
    ranges: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start >= _MIN_DURATION_SECONDS:
            ranges.append((start, end))
    return ranges


class _MatchEditWorker(Worker):
    """Shared body. Subclasses differ only in `_invert` and their name."""

    # False: keep the matched ranges. True: keep everything except them.
    _invert: bool = False

    @property
    @abstractmethod
    def name(self) -> str: ...

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        if params:
            raise InvalidMediaParamsError(
                f"{self.name} takes no parameters; what to look for goes on "
                f"find_content's 'query'. Got: {', '.join(sorted(params))}"
            )

        payload = _load_matches(previous_output, self.name)
        query = payload.get("query") if isinstance(payload, dict) else None
        preview = is_preview(message)

        with materialize_to_tempfile(resolve_input_uri(message, previous_output)) as source:
            probe_data = await probe(source)
            duration = max(0.0, float(probe_data.get("format", {}).get("duration") or 0.0))
            has_audio = any(
                s.get("codec_type") == "audio" for s in probe_data.get("streams", [])
            )

            matches = _clamped_matches(payload, duration)
            if not matches:
                raise InvalidMediaParamsError(self._nothing_found_message(query))

            keep = (
                complement(matches, duration=duration) if self._invert else matches
            )
            keep = [(s, e) for s, e in keep if e - s >= _MIN_DURATION_SECONDS]
            if not keep:
                raise InvalidMediaParamsError(self._nothing_left_message(query))

            filtergraph, outputs = build_keep_filtergraph(
                keep, has_audio=has_audio, preview=preview
            )
            args = ["-i", str(source), "-filter_complex", filtergraph, *outputs]
            args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
            if preview:
                args += list(PREVIEW_OUTPUT_ARGS)
            else:
                args += ["-preset", get_settings().video_encode_preset]

            with output_tempfile(".mp4") as destination:
                await run_ffmpeg([*args, str(destination)])
                if not destination.is_file() or destination.stat().st_size == 0:
                    raise InvalidMediaParamsError(f"{self.name} produced no output")
                produced = put_asset(destination, kind=AssetKind.VIDEO)

        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))

    @abstractmethod
    def _nothing_found_message(self, query: str | None) -> str: ...

    @abstractmethod
    def _nothing_left_message(self, query: str | None) -> str: ...


class RemoveMatchesWorker(_MatchEditWorker):
    name = "remove_matches"
    _invert = True

    def _nothing_found_message(self, query: str | None) -> str:
        described = f" matching {query!r}" if query else ""
        return (
            f"find_content found nothing{described}, so there is nothing to "
            "remove -- try describing it differently, or check it is actually "
            "visible in this video"
        )

    def _nothing_left_message(self, query: str | None) -> str:
        described = f" matching {query!r}" if query else ""
        return (
            f"everything{described} was found in every part of this video, so "
            "removing it would leave nothing behind"
        )


class KeepMatchesWorker(_MatchEditWorker):
    name = "keep_matches"
    _invert = False

    def _nothing_found_message(self, query: str | None) -> str:
        described = f" matching {query!r}" if query else ""
        return (
            f"find_content found nothing{described}, so there is nothing to "
            "keep -- try describing it differently, or check it is actually "
            "visible in this video"
        )

    def _nothing_left_message(self, query: str | None) -> str:  # pragma: no cover
        # Unreachable in practice: _clamped_matches already dropped anything
        # shorter than the floor, so a non-empty match list always yields at
        # least one keep segment. Present so the base class contract holds.
        described = f" matching {query!r}" if query else ""
        return f"the ranges{described} were too short to keep"
