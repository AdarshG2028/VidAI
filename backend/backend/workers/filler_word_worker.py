"""FillerWordWorker — flags disfluencies already present in the
transcript (Phase 10).

Deliberately unambiguous patterns only (um, uh, erm, hmm, mhm). Words
like "like"/"so"/"actually" are filler maybe a third of the time --
matching them indiscriminately would make the output look broken in
exactly the demo this exists for. Distinguishing "I like it" from filler
"like" needs an LLM pass over the text, a different and larger feature,
not attempted here. (Verified live before building this at all: Groq's
whisper-large-v3 does transcribe "um"/"uh" verbatim rather than silently
cleaning them, via a synthesized clip containing both -- worth recording
since Whisper models are also well known to sometimes drop them.)

Runs at segment granularity, the only granularity this project's Groq
integration currently requests (timestamp_granularities=["segment"]), so
a flagged instance's time window is its containing segment, not the
exact word -- good enough for "here's roughly where to look", not for
frame-accurate removal.

No caching, unlike transcribe/detect_scenes. Two independent reasons:
this needs a transcript to already exist, so it can never run at stage 0
-- primary_video(carried) is never None once transcribe has run before
it, so the shared cache gate would always decline. And it wouldn't need
one anyway: this is a regex scan over text already in hand, no ffmpeg
pass and no API call, so there's nothing expensive here to save.
"""

import json
import re
from typing import Any

from backend.storage import get_storage
from backend.workers.base import StageMessage, Worker
from backend.workers.media import (
    AssetKind,
    InvalidMediaParamsError,
    assets_payload,
    forward_assets,
    previous_assets,
    put_asset_bytes,
    stage_params,
)

_FILLER_RE = re.compile(r"\b(um+|uh+|erm+|hmm+|mhm+)\b", re.IGNORECASE)


class FillerWordWorker(Worker):
    name = "detect_filler_words"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        params = stage_params(message)
        if params:
            raise InvalidMediaParamsError(
                f"detect_filler_words takes no parameters; got {', '.join(sorted(params))}"
            )

        carried = previous_assets(previous_output)
        transcript_asset = next(
            (a for a in carried if a.kind == AssetKind.TRANSCRIPT), None
        )
        if transcript_asset is None:
            raise InvalidMediaParamsError(
                "detect_filler_words needs a 'transcribe' stage earlier in the "
                "workflow to supply a transcript"
            )

        transcript = json.loads(get_storage().get(transcript_asset.uri))
        instances = []
        for segment in transcript.get("segments", []):
            text = segment.get("text", "")
            for match in _FILLER_RE.finditer(text):
                instances.append(
                    {
                        "word": match.group(0),
                        "segment_start": segment.get("start"),
                        "segment_end": segment.get("end"),
                        "segment_text": text,
                    }
                )

        filler_words = put_asset_bytes(
            json.dumps({"instances": instances}, indent=2).encode("utf-8"),
            "filler_words.json",
            AssetKind.FILLER_WORDS,
        )

        return assets_payload(forward_assets(carried, [filler_words]))
