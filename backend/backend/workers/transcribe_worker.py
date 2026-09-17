"""TranscribeWorker — speech to a transcript and a subtitle file (5F).

Produces two new assets and passes the video through **untouched**: this
stage never re-encodes, so its output video is the same stored object it
was handed. That is what lets `transcribe` sit anywhere in a workflow
without costing a generation of quality.

Splitting transcription from burn-in (rather than one `subtitle` stage) is
what makes the `.srt` a first-class artifact a user can download and take
elsewhere — YouTube and every social platform accept a subtitle file
directly, and burned-in pixels can never be turned back into one.
"""

import json
import logging
from typing import Any

from backend.core.config import get_settings
from backend.services.transcription_client import (
    GroqTranscriptionClient,
    TranscriptionClient,
    TranscriptionError,
    TranscriptSegment,
    UnusableAudioError,
)
from backend.workers.base import StageMessage, Worker
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
    primary_video,
    put_asset_bytes,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
    stage_video_ids,
)
from backend.workers.video_asset_cache import SqlVideoAssetCache, VideoAssetCache

logger = logging.getLogger(__name__)

# Whisper works on 16kHz mono; anything richer is discarded by the model
# anyway. At 32kbps this is roughly 14MB per hour of speech, comfortably
# inside Groq's upload cap -- which is the reason the video itself is
# never uploaded.
_AUDIO_ARGS = ["-vn", "-ar", "16000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k"]


class TranscribeWorker(Worker):
    name = "transcribe"

    def __init__(
        self,
        client: TranscriptionClient | None = None,
        cache: VideoAssetCache | None = None,
    ) -> None:
        # Injectable so tests exercise the real audio extraction and SRT
        # building without calling a paid API over the network.
        self._client = client
        # Same reasoning, same default-to-real-implementation shape as
        # _client above. VideoAssetCache is generic (backend/workers/
        # video_asset_cache.py) -- transcribe was its only user until
        # detect_scenes needed the identical shape.
        self._cache = cache if cache is not None else SqlVideoAssetCache()

    def _resolve_client(self) -> TranscriptionClient:
        if self._client is not None:
            return self._client
        settings = get_settings()
        if not settings.groq_api_key:
            raise MediaProcessingError(
                "transcription needs GROQ_API_KEY; the worker cannot transcribe "
                "without it"
            )
        return GroqTranscriptionClient(
            api_key=settings.groq_api_key, model=settings.groq_transcription_model
        )

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - {"language"}
        if unknown:
            raise InvalidMediaParamsError(
                f"transcribe got unknown parameter(s): {', '.join(sorted(unknown))}"
            )
        language = params.get("language")
        if language is not None and (not isinstance(language, str) or not language.strip()):
            raise InvalidMediaParamsError(
                f"transcribe's 'language' must be a language code like 'en'; got {language!r}"
            )

        source_uri = resolve_input_uri(message, previous_output)
        carried = previous_assets(previous_output)

        # Caching only ever applies to the pristine upload: primary_video
        # being None on the carried assets means nothing upstream produced
        # (and therefore nothing re-encoded) a video, which is exactly the
        # "we're reading the original audio" case the roadmap calls out
        # ("Transcribe once, reuse across previews, re-runs, and final
        # renders"). A language override changes what transcription
        # produces, so it opts out of both reading and writing the cache
        # rather than serving or storing a mismatched result under the
        # same key.
        cache_key = None
        if language is None and primary_video(carried) is None:
            ids = stage_video_ids(message)
            if ids and ids[0]:
                cache_key = ids[0]

        if cache_key is not None:
            try:
                cached_transcript = await self._cache.get(cache_key, AssetKind.TRANSCRIPT)
                # Only bother asking for srt once transcript is confirmed
                # present -- either miss already means "not cacheable",
                # and both must be present together (see below) for this
                # to count as a hit at all.
                cached_srt = (
                    await self._cache.get(cache_key, AssetKind.SRT)
                    if cached_transcript is not None
                    else None
                )
            except Exception:
                # Same reasoning as the put() failure below: a caching
                # side-effect must never fail a stage that a real
                # transcription would have completed successfully. Treated
                # as a miss -- worst case, this call pays for a
                # transcription the cache could have skipped.
                logger.warning(
                    "failed to read cached transcript for video %s", cache_key, exc_info=True
                )
                cached_transcript = cached_srt = None
            # Both or neither: a transcript with no matching srt (an
            # interrupted write, or a row some future capability wrote
            # only one kind of) must fall through to real transcription,
            # not return a half-populated asset set.
            if cached_transcript is not None and cached_srt is not None:
                return _finish(carried, source_uri, cached_transcript, cached_srt)

        audio = await _extract_audio(source_uri)

        try:
            result = await self._resolve_client().transcribe(
                audio, filename="audio.mp3", language=language
            )
        except UnusableAudioError as exc:
            # The audio is the problem; identical bytes will fail the same
            # way, so this belongs in the DLQ rather than the retry queue.
            raise InvalidMediaParamsError(str(exc)) from exc
        except TranscriptionError as exc:
            raise MediaProcessingError(str(exc)) from exc

        if not result.segments:
            raise InvalidMediaParamsError(
                "no speech found in this video, so there is nothing to transcribe"
            )

        transcript = put_asset_bytes(
            json.dumps(
                {
                    "text": result.text,
                    "language": result.language,
                    "segments": [
                        {"start": s.start, "end": s.end, "text": s.text}
                        for s in result.segments
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
            "transcript.json",
            AssetKind.TRANSCRIPT,
        )
        srt = put_asset_bytes(
            build_srt(result.segments).encode("utf-8"), "captions.srt", AssetKind.SRT
        )

        if cache_key is not None:
            try:
                await self._cache.put(cache_key, AssetKind.TRANSCRIPT, transcript)
                await self._cache.put(cache_key, AssetKind.SRT, srt)
            except Exception:
                # A caching side-effect must never fail a stage whose
                # transcription itself succeeded -- worst case, the next
                # call pays for a re-transcription it could have skipped.
                logger.warning(
                    "failed to cache transcript for video %s", cache_key, exc_info=True
                )

        return _finish(carried, source_uri, transcript, srt)


def _finish(carried: list[Asset], source_uri: str, transcript: Asset, srt: Asset) -> dict[str, Any]:
    """Assemble the result payload, shared by the cache-hit and
    freshly-transcribed paths so they cannot silently diverge.

    A cache hit still needs the same "insert the video asset when nothing
    was carried" rule as a real transcription: both are stage 0 or its
    equivalent, and skipping this on the cache-hit path would drop the
    video from what reaches the next stage.
    """
    produced = [transcript, srt]
    if not carried:
        produced.insert(0, Asset(kind=AssetKind.VIDEO, uri=source_uri))
    return assets_payload(forward_assets(carried, produced))


async def _extract_audio(source_uri: str) -> bytes:
    with materialize_to_tempfile(source_uri) as source:
        with output_tempfile(".mp3") as destination:
            await run_ffmpeg(["-i", str(source), *_AUDIO_ARGS, str(destination)])
            if not destination.is_file() or destination.stat().st_size == 0:
                raise InvalidMediaParamsError(
                    "this video has no audio track to transcribe"
                )
            return destination.read_bytes()


def build_srt(segments: list[TranscriptSegment]) -> str:
    """Render segments as SubRip.

    Cues are 1-indexed with `HH:MM:SS,mmm` timestamps and a blank line
    between them; ffmpeg's subtitles filter and every player are strict
    about that shape, including the trailing newline.
    """
    blocks = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n{_timestamp(segment.start)} --> {_timestamp(segment.end)}\n"
            f"{segment.text}\n"
        )
    return "\n".join(blocks)


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    whole = int(seconds)
    milliseconds = int(round((seconds - whole) * 1000))
    if milliseconds == 1000:  # rounding carried into the next second
        whole, milliseconds = whole + 1, 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"
