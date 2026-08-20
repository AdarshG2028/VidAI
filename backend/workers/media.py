"""Shared media helpers every Phase 5 capability builds on.

Phase 5's capabilities (crop, color, audio, transcribe, render, ...) all
repeat the same shape: work out which video this stage should read, pull
it out of storage, run ffmpeg over it, put the result back, and describe
what was produced for the next stage. That procedure lives here once
rather than five-plus times over.

**Assets, not "the video."** A stage no longer implicitly produces one
video -- it produces a list of typed Assets, because some stages
genuinely don't produce a video at all: transcribe emits a transcript and
an .srt and passes the video through untouched. See the architecture
doc, §19 Phase 5 / Changelog v10.

**Chaining.** Setu's engine forwards the *same* job-submission payload to
every stage, so stage_params[i]["video_uris"] always points at the
originally uploaded file -- never at what the previous stage produced.
The channel that does vary per stage is previous_output (the prior
stage's Result payload, injected by StageProcessingService), so that is
what carries assets forward, per the Worker contract in base.py.

forward_assets() implements *monotonic accumulation*: whatever a stage
received it passes on, replacing only the kinds it actually reproduced.
That is what frees a consuming stage from having to sit adjacent to its
producing stage -- burn_subtitles works whether or not crop ran between
it and transcribe.

This module is deliberately split: everything here is pure (no storage,
no subprocess, no event loop), so it is testable with no ffmpeg and no
infrastructure. The ffmpeg/storage half arrives alongside it and depends
on these definitions, not the other way round.
"""

import asyncio
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.config import get_settings
from backend.storage import StorageObjectNotFoundError, get_storage
from backend.workers.base import PermanentError, StageMessage

__all__ = [
    "PREVIEW_OUTPUT_ARGS",
    "PREVIEW_SCALE_FILTER",
    "Asset",
    "AssetKind",
    "InvalidMediaParamsError",
    "MediaProcessingError",
    "assets_payload",
    "extract_video_metadata",
    "forward_assets",
    "is_preview",
    "materialize_to_tempfile",
    "output_tempfile",
    "previous_assets",
    "primary_video",
    "probe",
    "process_video",
    "put_asset",
    "resolve_input_uri",
    "run_ffmpeg",
    "stage_params",
    "stage_video_uris",
    "video_stream",
]

# Longest tail of ffmpeg/ffprobe stderr kept in an error message. Enough to
# carry the actual diagnostic (which ffmpeg prints last) without dumping an
# entire encode log into Job.last_error.
_STDERR_TAIL = 500

# Streaming copy size. Big enough that syscall overhead is irrelevant,
# small enough that resident memory stays flat regardless of file size.
_COPY_CHUNK_SIZE = 1024 * 1024

# Preview caps output height rather than width so portrait and landscape
# both shrink, and never upscales a source already smaller than the cap.
# -2 keeps the other dimension even, which h264 requires. The comma inside
# min() is escaped because commas separate filters in a filtergraph.
#
# Exported (not module-private) so a capability that can't use
# process_video -- because it needs a filter_complex rather than one
# linear -vf/-af chain, e.g. remove_segment_worker.py -- can still honour
# preview mode instead of silently ignoring it.
_PREVIEW_HEIGHT = 480
PREVIEW_SCALE_FILTER = rf"scale=-2:min(ih\,{_PREVIEW_HEIGHT})"
PREVIEW_OUTPUT_ARGS = ("-preset", "ultrafast", "-crf", "30")

# Set by compile_workflow when a proposal is submitted in preview mode.
PREVIEW_FLAG = "_preview"


class MediaProcessingError(Exception):
    """A media operation failed for a reason a retry might fix -- ffmpeg
    missing from PATH, a subprocess killed, storage briefly unreachable.
    Plain Exception semantics on purpose: the harness retries it with
    backoff. Mirrors video_analysis_worker.VideoAnalysisError."""


class InvalidMediaParamsError(MediaProcessingError, PermanentError):
    """The stage's own inputs are unusable -- a malformed aspect ratio, a
    missing required param, no input video to work from. The same message
    would fail identically on every redelivery, so the harness DLQs it on
    the first attempt rather than spending the retry budget. Mirrors
    video_analysis_worker.UnsupportedVideoError."""


class AssetKind:
    """The asset kinds Phase 5 produces.

    `Asset.kind` stays a plain str rather than an enum: Phase 10's
    semantic outputs (scenes, faces, embeddings) are meant to be additive
    new kinds, and an enum would make each one a change here plus a
    migration of anything that persisted the old set.
    """

    VIDEO = "video"
    TRANSCRIPT = "transcript"
    SRT = "srt"
    AUDIO = "audio"
    THUMBNAIL = "thumbnail"
    SCENES = "scenes"
    FILLER_WORDS = "filler_words"


@dataclass(frozen=True)
class Asset:
    """One artifact a stage produced, addressed by storage URI.

    Frozen dataclass matching StageMessage/Proposal's convention
    (backend/workers/base.py, backend/services/proposal.py) -- Pydantic
    stays this repo's API-schema layer, not its internal-domain layer.
    Serialized to plain dicts only at the Result.payload JSON boundary.
    """

    kind: str
    uri: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "uri": self.uri}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Asset":
        """Raises KeyError on a malformed entry rather than skipping it:
        this only ever parses our own serialization, so a missing field
        is a bug worth surfacing loudly, not data to route around."""
        return cls(kind=data["kind"], uri=data["uri"])


def _stage_entry(message: StageMessage) -> dict[str, Any]:
    """This stage's slice of the compiled payload (workflow_compiler.py
    writes `{"stage_params": {"<i>": {"params": ..., "video_uris": ...}}}`).
    Tolerates absence: `dummy` and video_analysis jobs carry a payload
    with no stage_params at all."""
    entry = message.payload.get("stage_params", {}).get(str(message.stage), {})
    return entry if isinstance(entry, dict) else {}


def stage_params(message: StageMessage) -> dict[str, Any]:
    """The params the planner chose for *this* stage."""
    return _stage_entry(message).get("params", {})


def stage_video_uris(message: StageMessage) -> list[str]:
    """The originally uploaded videos this stage was compiled against.

    Only meaningful at stage 0: every later stage takes its real input
    from previous_output, and the compiler fills these in from the same
    fixed handle->URI map regardless of stage index. merge (a first-stage
    capability) is the one consumer of the full list; everything else
    goes through resolve_input_uri.
    """
    return _stage_entry(message).get("video_uris", [])


def stage_video_ids(message: StageMessage) -> list[str]:
    """The Video row ids backing stage_video_uris, same order.

    Absent (empty string per entry, or the whole list missing) for any
    payload that predates this convention or was built by hand rather
    than through compile_workflow -- callers that use this for cache
    keys must already treat "" as "no id" the same way they treat an
    empty list, never assume a positional entry is real.
    """
    return _stage_entry(message).get("video_ids", [])


def previous_assets(previous_output: dict[str, Any] | None) -> list[Asset]:
    """The prior stage's assets, or [] when there is no prior stage.

    Also [] when the prior stage predates this convention (dummy,
    video_analysis) -- those return payloads with no "assets" key, which
    correctly falls back to the original upload via resolve_input_uri.
    """
    if not previous_output:
        return []
    return [Asset.from_dict(item) for item in previous_output.get("assets", [])]


def primary_video(assets: list[Asset]) -> Asset | None:
    """The chainable video in an asset list.

    Every capability selects its input through this one function so no
    two of them can disagree about the rule. Exactly one video asset is
    expected; the first wins if that ever stops holding.
    """
    return next((asset for asset in assets if asset.kind == AssetKind.VIDEO), None)


def forward_assets(previous: list[Asset], produced: list[Asset]) -> list[Asset]:
    """Monotonic accumulation -- carry everything forward, replacing only
    the kinds this stage reproduced.

    So color, which reproduces the video and nothing else, still hands an
    .srt produced three stages earlier to whatever comes next. Ordering
    within the list carries no meaning (primary_video selects by kind),
    so carried-then-produced is used for its simplicity: it stays correct
    when a stage emits several assets of one kind, which in-place
    substitution would not.
    """
    reproduced_kinds = {asset.kind for asset in produced}
    carried = [asset for asset in previous if asset.kind not in reproduced_kinds]
    return carried + list(produced)


def assets_payload(assets: list[Asset]) -> dict[str, Any]:
    """The dict shape a capability returns from process().

    StageProcessingService persists this as Result.payload and lifts the
    primary video's URI into Result.artifact_uri.
    """
    return {"assets": [asset.to_dict() for asset in assets]}


def resolve_input_uri(
    message: StageMessage, previous_output: dict[str, Any] | None
) -> str:
    """The video this stage should read: the previous stage's output if
    there is one, otherwise the originally uploaded video.

    The fallback is what makes a capability work identically as stage 0
    of its own job or as stage 4 of a chain, with no per-capability
    branching.
    """
    chained = primary_video(previous_assets(previous_output))
    if chained is not None:
        return chained.uri

    uris = stage_video_uris(message)
    if not uris:
        # Indexed defensively: this runs on a failure path, and an
        # IndexError raised while building the message would bury the
        # actual problem.
        name = (
            message.workflow[message.stage]
            if message.stage < len(message.workflow)
            else "?"
        )
        raise InvalidMediaParamsError(
            f"stage {message.stage} ({name}) has no input video: nothing was "
            "produced upstream and no video_uris were compiled for it"
        )
    return uris[0]


def is_preview(message: StageMessage) -> bool:
    """Whether this job was submitted as a preview.

    A preview is not a different pipeline -- it is the same workflow
    compiled in a different mode and submitted as an ordinary Job, so it
    inherits retry/DLQ/idempotency/observability unchanged. The flag is
    honoured in exactly one place (process_video), which is what stops
    any individual capability from forgetting to.
    """
    return bool(message.payload.get(PREVIEW_FLAG))


# --- local materialization -------------------------------------------------


@contextmanager
def materialize_to_tempfile(uri: str, suffix: str | None = None) -> Iterator[Path]:
    """Download a stored object to a real local file for ffmpeg to read.

    ffmpeg needs a path, and Storage is deliberately byte-oriented (an
    S3 backend would need this same download step), so this is the one
    place a capability touches local disk.

    delete=False plus an explicit close before yielding, rather than
    NamedTemporaryFile's default: its delete-on-close keeps the handle
    open -- and on Windows, exclusively locked -- for the lifetime of the
    block, and a separate ffmpeg process cannot open a file this process
    still holds. It must be closed before the subprocess runs, not merely
    flushed. Same reasoning as video_analysis_worker.py.
    """
    try:
        source = get_storage().open_stream(uri)
    except StorageObjectNotFoundError as exc:
        # The object won't materialize on a later attempt, so retrying
        # would burn the budget for nothing.
        raise InvalidMediaParamsError(f"input asset not found in storage: {uri}") from exc

    tmp = tempfile.NamedTemporaryFile(suffix=suffix or Path(uri).suffix or ".mp4", delete=False)
    try:
        # Copied in chunks rather than via storage.get(), which returns the
        # whole object as bytes. This runs for every stage of every job, so
        # a 200MB video meant a 200MB resident spike per worker per stage
        # (measured) -- enough to OOM a small instance running several
        # workers, for a file that is only ever going straight to disk.
        with source:
            while chunk := source.read(_COPY_CHUNK_SIZE):
                tmp.write(chunk)
        tmp.close()
        yield Path(tmp.name)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@contextmanager
def output_tempfile(suffix: str = ".mp4") -> Iterator[Path]:
    """A path for ffmpeg to write its output to.

    A temp *directory* rather than a temp file: ffmpeg wants to create
    the output itself, and handing it an already-existing empty file
    invites container-specific complaints. Nothing here ever opens the
    path, so the Windows locking problem above doesn't arise.
    """
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory) / f"output{suffix}"


def put_asset(path: Path, kind: str = AssetKind.VIDEO) -> Asset:
    """Upload a locally-produced file and describe it as an Asset."""
    # put_file rather than put(path.read_bytes()): the output of a render
    # can be hundreds of megabytes, and reading it into memory purely to
    # hand it to storage is the same waste as on the read side.
    return Asset(kind=kind, uri=get_storage().put_file(path, suggested_name=path.name))


def put_asset_bytes(data: bytes, filename: str, kind: str) -> Asset:
    """Store generated bytes as an asset, for capabilities whose output
    was never a file on disk (transcribe's transcript.json/.srt, Phase
    10's scenes.json) -- put_asset above exists for the common case of
    something ffmpeg already wrote to a path."""
    return Asset(kind=kind, uri=get_storage().put(data, suggested_name=filename))


# --- subprocess wrappers ---------------------------------------------------


async def run_ffmpeg(args: list[str], *, cwd: Path | None = None) -> str:
    """Run ffmpeg and return its stderr.

    `cwd` exists for filters that take a file path as a filter *argument*
    rather than as an input -- ffmpeg's `subtitles=` being the one that
    matters here. Those paths are parsed by the filtergraph lexer, where
    a backslash escapes and a colon separates options, so a Windows path
    like C:\\Users\\...\\c.srt is mangled beyond rescue (verified: the
    backslashes vanish and everything after "C" is read as an option).
    Running from the file's own directory and naming it bare sidesteps
    the escaping problem entirely, and does so identically on every OS.

    stderr is returned rather than discarded because ffmpeg reports its
    analysis filters (signalstats, loudnorm, volumedetect) there -- which
    is how capabilities and tests read measurements back out without
    adding a pixel/audio library to this project.

    -nostdin stops ffmpeg consuming the parent's stdin and hanging in a
    non-interactive context; -y lets it write the output path it was
    given.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-y",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        # Environment problem, not input: a redeploy or a retry landing on
        # a different worker instance could still succeed, so this stays
        # retryable.
        raise MediaProcessingError("ffmpeg is not installed or not on PATH") from exc

    _, stderr = await proc.communicate()
    text = stderr.decode(errors="replace")
    if proc.returncode != 0:
        # Treated as permanent for the same reason video_analysis_worker
        # treats a failed ffprobe that way: the dominant cause is the
        # input or the params, which fail identically on every redelivery.
        # A genuinely transient failure here (OOM, disk full) is DLQ'd on
        # the first attempt too -- accepted, since reliably telling the
        # two apart from stderr text isn't worth the false confidence.
        raise InvalidMediaParamsError(f"ffmpeg failed: {text[-_STDERR_TAIL:]}")
    return text


async def probe(path: Path) -> dict[str, Any]:
    """ffprobe metadata for a local file, as parsed JSON.

    Shared with video_analysis_worker.py, which translates the
    MediaProcessingError/InvalidMediaParamsError this raises into its own
    VideoAnalysisError/UnsupportedVideoError at its own boundary, to keep
    its existing tested retry/DLQ contract without a second implementation
    of the same dozen lines of argv.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaProcessingError("ffprobe is not installed or not on PATH") from exc

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise InvalidMediaParamsError(
            f"ffprobe failed: {stderr.decode(errors='replace')[-_STDERR_TAIL:]}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise InvalidMediaParamsError(f"ffprobe produced unparseable output: {exc}") from exc


def video_stream(probe_data: dict[str, Any]) -> dict[str, Any]:
    """The first video stream, for capabilities that need source geometry
    (crop computing a centred rect, pad computing borders)."""
    stream = next(
        (s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if stream is None:
        raise InvalidMediaParamsError("input has no video stream")
    return stream


def extract_video_metadata(probe_data: dict[str, Any]) -> dict[str, Any]:
    """duration/resolution/orientation/fps/codec from a probe() result.

    Was video_analysis_worker.py's own private `_extract_metadata`, backed
    by a second, near-identical ffprobe-running implementation. Moved here
    so upload-time analysis and any later re-measurement of an edited
    video (backend/services/video_chain.py's `measure`) share one
    implementation rather than three ways of reading the same JSON.
    Raises InvalidMediaParamsError (via video_stream) when there is no
    video stream -- video_analysis_worker.py translates that into its own
    UnsupportedVideoError to keep its existing retry/DLQ contract.
    """
    stream = video_stream(probe_data)
    width = stream.get("width")
    height = stream.get("height")
    return {
        "duration_seconds": _parse_duration(probe_data),
        "fps": _parse_frame_rate(stream.get("r_frame_rate")),
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}" if width and height else None,
        "orientation": _orientation(width, height),
        "codec": stream.get("codec_name"),
    }


def _parse_duration(probe_data: dict[str, Any]) -> float | None:
    raw = probe_data.get("format", {}).get("duration")
    if raw is None:
        return None
    try:
        return round(float(raw), 3)
    except (TypeError, ValueError):
        return None


def _parse_frame_rate(raw: str | None) -> float | None:
    """ffprobe reports frame rate as a "num/den" ratio string, not a float."""
    if not raw:
        return None
    num, _, den = raw.partition("/")
    den = den or "1"
    try:
        denominator = float(den)
        return round(float(num) / denominator, 3) if denominator else None
    except ValueError:
        return None


def _orientation(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    return "portrait" if height > width else "landscape"


# --- the workhorse ---------------------------------------------------------


async def process_video(
    message: StageMessage,
    previous_output: dict[str, Any] | None,
    *,
    input_args: list[str] | None = None,
    video_filters: list[str] | None = None,
    audio_filters: list[str] | None = None,
    output_args: list[str] | None = None,
    suffix: str = ".mp4",
) -> Asset:
    """Read this stage's input video, run ffmpeg over it, store the result.

    This is what keeps each capability to roughly "params in, filter list
    out": crop, color, trim and friends never touch storage, temp files,
    subprocesses or the preview flag themselves.

    Streams the caller didn't filter are copied rather than re-encoded --
    so a colour adjustment leaves the audio bit-identical and skips the
    cost of transcoding it. In preview mode the video is always
    re-encoded, since a scale filter is being added.

    Assumes an h264-compatible container (.mp4), which every 5B-5D
    capability outputs. Anything wanting a different encoder (render's
    GIF/MOV targets) drives run_ffmpeg directly.
    """
    preview = is_preview(message)

    video_filters = list(video_filters or [])
    if preview:
        video_filters.append(PREVIEW_SCALE_FILTER)

    args: list[str] = [*(input_args or [])]
    with materialize_to_tempfile(resolve_input_uri(message, previous_output)) as source:
        args += ["-i", str(source)]

        if video_filters:
            args += ["-vf", ",".join(video_filters)]
        else:
            args += ["-c:v", "copy"]

        if audio_filters:
            args += ["-af", ",".join(audio_filters)]
        else:
            args += ["-c:a", "copy"]

        if preview:
            args += list(PREVIEW_OUTPUT_ARGS)
        else:
            # Placed before output_args so a capability that needs a
            # specific preset can still override it -- later flags win.
            args += ["-preset", get_settings().video_encode_preset]
        args += list(output_args or [])

        with output_tempfile(suffix) as destination:
            args.append(str(destination))
            await run_ffmpeg(args)

            if not destination.is_file() or destination.stat().st_size == 0:
                # ffmpeg can exit 0 having written nothing (e.g. a trim
                # range entirely past the end of the input). Catching it
                # here keeps a zero-byte "video" from being stored and
                # handed to the next stage as if it were real.
                raise InvalidMediaParamsError(
                    "ffmpeg reported success but produced no output — check the "
                    "stage's params against the input"
                )
            return put_asset(destination)
