"""AudioWorker — loudness normalisation and silence trimming (Phase 5D).

Two operations that are almost always wanted together on raw footage:
inconsistent volume and a dead pause before anyone starts talking are the
two most audible marks of an unedited clip.

Silence trimming is why this worker is longer than the 5B/5C ones. ffmpeg's
`silenceremove` filter only touches the audio stream, so using it directly
would shorten the audio while the video kept its original length -- the
result drifts out of sync and ends on a frozen tail. Correct trimming has
to cut both streams by the same amount, which means finding the silence
first (silencedetect) and then trimming with matched `trim`/`atrim`
filters. That costs one extra read of the input, and only when silence
removal is actually requested.
"""

import re
from typing import Any

from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    materialize_to_tempfile,
    previous_assets,
    probe,
    process_video,
    resolve_input_uri,
    run_ffmpeg,
    stage_params,
)

_ACTIONS = ("normalize", "remove_silence", "preserve_music")

# EBU R128 defaults. -16 LUFS is the de-facto target for online video --
# louder than broadcast's -23, which is what makes broadcast audio feel
# quiet next to everything else in a social feed.
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"

# Anything under this counts as silence. -50dB is permissive enough to
# treat room tone and encoder noise as silence rather than as content.
_SILENCE_THRESHOLD_DB = -50
_MIN_SILENCE_SECONDS = 0.3

# Never trim a clip down to less than this. A recording that is silent
# throughout would otherwise trim to nothing and produce a zero-length
# "video" that the next stage would have to deal with.
_MIN_REMAINING_SECONDS = 0.5

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)")


class AudioWorker(Worker):
    name = "audio"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        unknown = set(params) - set(_ACTIONS)
        if unknown:
            raise InvalidMediaParamsError(
                f"audio got unknown parameter(s): {', '.join(sorted(unknown))}"
            )
        for name in _ACTIONS:
            if name in params and not isinstance(params[name], bool):
                raise InvalidMediaParamsError(
                    f"audio's '{name}' must be true or false; got {params[name]!r}"
                )

        normalize = bool(params.get("normalize"))
        # preserve_music suppresses silence removal rather than doing
        # anything clever: telling a deliberate musical rest from dead air
        # needs content classification this phase does not have, so the
        # honest behaviour is to leave the audio alone and say so.
        remove_silence = bool(params.get("remove_silence")) and not bool(
            params.get("preserve_music")
        )

        if not normalize and not remove_silence:
            raise InvalidMediaParamsError(
                "audio needs normalize or remove_silence to be true "
                "(remove_silence is ignored when preserve_music is set)"
            )

        video_filters: list[str] = []
        audio_filters: list[str] = []

        if remove_silence:
            window = await _find_speech_window(
                resolve_input_uri(message, previous_output)
            )
            if window is not None:
                start, end = window
                # Matched trims keep the streams aligned; setpts/asetpts
                # rebase the timestamps so the result starts at zero
                # instead of retaining a gap where the trimmed head was.
                video_filters += [f"trim=start={start}:end={end}", "setpts=PTS-STARTPTS"]
                audio_filters += [f"atrim=start={start}:end={end}", "asetpts=PTS-STARTPTS"]

        if normalize:
            # After any atrim, so loudness is measured over the audio that
            # will actually survive rather than including the silence.
            audio_filters.append(_LOUDNORM)

        produced = await process_video(
            message,
            previous_output,
            video_filters=video_filters,
            audio_filters=audio_filters,
        )
        return assets_payload(forward_assets(previous_assets(previous_output), [produced]))


async def _find_speech_window(uri: str) -> tuple[float, float] | None:
    """The (start, end) of the clip once leading and trailing silence are
    removed, or None if there is nothing worth trimming.

    Only the head and tail are trimmed. Cutting silence out of the middle
    would mean splicing several segments back together, which is a
    different and much larger operation -- and one that quietly destroys
    timing for anything downstream that assumed a continuous timeline.
    """
    with materialize_to_tempfile(uri) as path:
        duration = float((await probe(path)).get("format", {}).get("duration") or 0.0)
        stderr = await run_ffmpeg(
            [
                "-i", str(path),
                "-af",
                f"silencedetect=noise={_SILENCE_THRESHOLD_DB}dB:d={_MIN_SILENCE_SECONDS}",
                "-f", "null", "-",
            ]
        )

    if duration <= 0:
        return None

    starts = [float(value) for value in _SILENCE_START.findall(stderr)]
    ends = [float(value) for value in _SILENCE_END.findall(stderr)]

    start = 0.0
    # A silence reported as beginning at (or fractionally before) zero is
    # the leading one; trim up to where it ends.
    if starts and starts[0] <= 0.05 and ends:
        start = ends[0]

    end = duration
    # A trailing silence is one whose start has no matching end before the
    # file runs out, or whose end coincides with it.
    if starts and starts[-1] > start and (len(ends) < len(starts) or ends[-1] >= duration - 0.05):
        end = starts[-1]

    if end - start < _MIN_REMAINING_SECONDS or (start <= 0.0 and end >= duration):
        return None
    return (round(start, 3), round(end, 3))
