"""VideoAnalysisWorker — the sole stage of Job #1.

Extracts a subset of the full metadata list from the architecture doc
(duration, fps, resolution, codec, orientation) via ffprobe; the rest
(transcript, faces, scene changes, camera motion, blur score, loudness)
comes later, each as its own additive change to media.extract_video_metadata,
once needed by a real planner prompt — proving the pipeline end to end
matters more up front than shipping every metric on day one.

Stateless per the Worker contract (see workers/base.py): reads the video
through the storage abstraction and returns a plain dict. It does not
write to the videos table itself — StageProcessingService persists
whatever this returns as stage 0's Result, exactly like any other worker.

Probing and metadata extraction are backend.workers.media's — this used to
carry a second, near-identical ffprobe-running implementation of its own;
now it translates media's generic exceptions into this worker's own
VideoAnalysisError/UnsupportedVideoError, which is the only part of the
old contract that's actually specific to this worker.
"""

from typing import Any

from backend.workers.base import PermanentError, StageMessage, Worker
from backend.workers.media import (
    InvalidMediaParamsError,
    MediaProcessingError,
    extract_video_metadata,
    materialize_to_tempfile,
    probe,
)


class VideoAnalysisError(Exception):
    """ffprobe itself couldn't run — e.g. not installed on this worker's
    PATH. An environment problem, not an input problem: a fixed deploy or
    a retry landing on a different worker instance could still succeed, so
    this stays retryable via the harness's normal retry/DLQ path.
    """


class UnsupportedVideoError(VideoAnalysisError, PermanentError):
    """The uploaded bytes themselves are what ffprobe can't handle — wrong
    format, corrupt file, no video stream. ffprobe will produce the exact
    same failure on every redelivery of these same bytes, so this is a
    PermanentError: skip the retry budget and DLQ on the first attempt
    instead of wasting the full backoff on a foregone conclusion.
    """


class VideoAnalysisWorker(Worker):
    name = "video_analysis"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        video_uri = message.payload["video_uri"]
        try:
            with materialize_to_tempfile(video_uri) as path:
                probe_data = await probe(path)
                return extract_video_metadata(probe_data)
        except MediaProcessingError as exc:
            # InvalidMediaParamsError (a MediaProcessingError subclass) is
            # always the permanent case -- bad/corrupt bytes or no video
            # stream, which fails identically on every retry -- so it must
            # be checked first; the bare MediaProcessingError branch below
            # would otherwise also catch it and misreport it as retryable.
            if isinstance(exc, InvalidMediaParamsError):
                raise UnsupportedVideoError(str(exc)) from exc
            raise VideoAnalysisError(str(exc)) from exc
