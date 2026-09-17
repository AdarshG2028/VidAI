"""Capability registry (§6): the one source of truth for which stages a
proposal may reference and what parameters each accepts. Doubles as the
future LLM prompt's contract ("here's what you're allowed to choose from")
and ProposalValidator's runtime contract ("here's what's actually
allowed").

A class, not a bare module-level dict, so ProposalValidator depends on this
interface rather than a global constant -- Phase 5 registers each real
editing capability here, one at a time, without touching validator logic.

Capabilities, not workers, is the seam the planner sees: it only ever
reads names, descriptions, parameter schemas and asset kinds from here,
and never learns that a Worker subclass or a Kafka topic exists. That is
what lets an implementation change (splitting a worker, swapping ffmpeg
for something else) happen without the planner noticing.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageCapability:
    name: str
    description: str
    # Known param keys -> expected Python type. Known-keys-plus-basic-type-
    # check is enough for V1 (§6); not a full JSON-Schema validator.
    parameter_schema: dict[str, type] = field(default_factory=dict)

    # What this stage needs to already exist, and what it leaves behind
    # (Phase 5's asset model -- see backend/workers/media.py). Kinds are
    # plain strings rather than an import of AssetKind: this registry is a
    # planner-facing declaration of what may be proposed, and pointing it
    # at a worker implementation module would invert that layering.
    # AssetKind lists the canonical spellings.
    #
    # The defaults describe the overwhelmingly common case -- takes a
    # video, returns a video -- so crop/color/audio/trim/merge/render all
    # need no extra declaration. Only the transcript pair diverges:
    # transcribe additionally produces "transcript"/"srt", and
    # burn_subtitles additionally requires "srt".
    #
    # validate_proposal walks these in order to reject a proposal that
    # consumes an asset nothing upstream produces, so the planner's
    # retry loop can fix it rather than the job dying at execution time.
    requires_asset_kinds: tuple[str, ...] = ("video",)
    produces_asset_kinds: tuple[str, ...] = ("video",)

    # True only for merge (backend/workers/merge_worker.py). Setu forwards
    # the original job-submission payload to every stage, so a video's real
    # uploads are only visible at stage 0 -- from stage 1 on, the true
    # input is whatever the previous stage produced, a single video. A
    # merge placed later would silently concatenate the *original* uploads
    # and discard every edit before it, so the worker refuses at runtime;
    # validate_proposal below checks it structurally instead, so the room
    # never votes on a proposal already guaranteed to dead-letter (observed
    # live: `[trim, merge]` got approved, ran, and dead-lettered instead of
    # being caught before anyone voted).
    must_be_first_stage: bool = False


class CapabilityRegistry:
    def __init__(self, capabilities: dict[str, StageCapability] | None = None) -> None:
        self._capabilities = dict(capabilities or {})

    def register(self, capability: StageCapability) -> None:
        self._capabilities[capability.name] = capability

    def exists(self, stage_name: str) -> bool:
        return stage_name in self._capabilities

    def get(self, stage_name: str) -> StageCapability | None:
        return self._capabilities.get(stage_name)

    def list(self) -> list[StageCapability]:
        return list(self._capabilities.values())


# `dummy` (backend/workers/dummy_worker.py) is a stand-in stage proving the
# validate -> compile -> submit -> execute wiring end to end, not a real
# editing operation. It stays registered after Phase 5 lands real
# capabilities: the Setu-core infrastructure tests (retry/DLQ, crash
# recovery) deliberately drive a worker that needs no ffmpeg.
#
# Real capabilities are registered here one sub-phase at a time (§19 Phase
# 5): crop/resize/rotate/flip/pad (5B), color (5C), audio (5D), trim/merge
# (5E), transcribe/burn_subtitles (5F), render (5G).
DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry(
    {
        "dummy": StageCapability(
            name="dummy",
            description=(
                "No-op stand-in stage used to prove the proposal pipeline "
                "end to end. Not a real editing operation."
            ),
            parameter_schema={},
            # Genuinely consumes and produces nothing -- the defaults
            # would claim a video it never reads, which now matters:
            # validate_proposal requires the first stage to name a video
            # when it declares that it needs one.
            requires_asset_kinds=(),
            produces_asset_kinds=(),
        ),
        # Descriptions here are the planner's entire understanding of a
        # capability -- it never sees the worker. So they say what the stage
        # is *for*, in the words a user would use ("vertical", "Reels"),
        # not how it is implemented.
        "crop": StageCapability(
            name="crop",
            description=(
                "Reframe the video to a target aspect ratio by cropping to the "
                "centre of the frame, filling it edge to edge with no black bars. "
                "Use for converting landscape footage to vertical for Reels, "
                "Shorts or TikTok ('9:16'), to square for feed posts ('1:1'), or "
                "back to widescreen ('16:9'). Content near the edges is cut off; "
                "use 'pad' instead when nothing may be lost."
            ),
            parameter_schema={"aspect_ratio": str},
        ),
        "color": StageCapability(
            name="color",
            description=(
                "Adjust the look of the picture: brightness, contrast, saturation, "
                "gamma and sharpening, in any combination. Use for footage that is "
                "too dark or flat, or to make it more vivid and punchy. Values are "
                "multipliers around a neutral point -- brightness 0 and contrast, "
                "saturation and gamma 1 mean 'unchanged', so pass only what should "
                "change. Typical edits are small: brightness 0.1, contrast 1.2, "
                "saturation 1.3. This does not move, resize or reframe the picture."
            ),
            parameter_schema={
                "brightness": float,
                "contrast": float,
                "saturation": float,
                "gamma": float,
                "sharpen": float,
            },
        ),
        "resize": StageCapability(
            name="resize",
            description=(
                "Scale the video to explicit pixel dimensions, e.g. 1920x1080 or "
                "720p. Give only width or only height to scale proportionally; "
                "give both to force an exact size, which may stretch the picture. "
                "Use this for a size requirement ('make it 1080p'); use 'crop' or "
                "'pad' instead for a shape requirement ('make it vertical')."
            ),
            parameter_schema={"width": int, "height": int},
        ),
        "rotate": StageCapability(
            name="rotate",
            description=(
                "Rotate the picture by a quarter turn: 90, 180 or 270 degrees "
                "clockwise. Use for footage filmed with the camera held the wrong "
                "way up. Only these three angles are supported."
            ),
            parameter_schema={"degrees": int},
        ),
        "flip": StageCapability(
            name="flip",
            description=(
                "Mirror the picture, either 'horizontal' (left-right) or "
                "'vertical' (upside down). Horizontal is the common one: it "
                "un-mirrors selfie-camera footage so text reads the right way."
            ),
            parameter_schema={"direction": str},
        ),
        "pad": StageCapability(
            name="pad",
            description=(
                "Fit the video to a target aspect ratio by adding bars around it, "
                "keeping the entire picture visible. The counterpart to 'crop': "
                "use pad when nothing may be cut off (a screen recording, a chart, "
                "anything with text at the edges), and crop when the frame should "
                "be filled edge to edge. pad_color defaults to black."
            ),
            parameter_schema={"aspect_ratio": str, "pad_color": str},
        ),
        "audio": StageCapability(
            name="audio",
            description=(
                "Clean up the soundtrack. 'normalize' evens out inconsistent "
                "volume and brings the clip to a standard loudness for online "
                "video -- use it whenever audio is too quiet, too loud, or jumps "
                "around. 'remove_silence' trims dead air from the start and end "
                "of the clip, so it begins the moment something happens. Set "
                "'preserve_music' when the audio is musical and its pauses are "
                "intentional; that turns silence trimming off. At least one of "
                "normalize or remove_silence must be true."
            ),
            parameter_schema={
                "normalize": bool,
                "remove_silence": bool,
                "preserve_music": bool,
            },
        ),
        "trim": StageCapability(
            name="trim",
            description=(
                "Cut the video down to a time range, given in seconds -- KEEPS "
                "only what's given, discards the rest. 'start' alone drops "
                "everything before it; 'end' alone drops everything after it; "
                "both together keep only what lies between. Use for removing a "
                "slow intro, cutting a rambling tail, or keeping one section of a "
                "longer recording. Can be used more than once in a workflow -- "
                "each trim applies to the result of the one before. Do NOT use "
                "this for 'cut out the middle part from X to Y' or 'remove the "
                "part between X and Y' -- that means DISCARDING a middle range "
                "and keeping both surrounding pieces, which is remove_segment, "
                "not trim."
            ),
            parameter_schema={"start": float, "end": float},
        ),
        "remove_segment": StageCapability(
            name="remove_segment",
            description=(
                "Cut a middle piece OUT of the video and rejoin the two "
                "surrounding pieces into one clip -- the opposite of trim, "
                "which keeps a range instead of removing it. Use this for 'cut "
                "out 0:40 to 1:10', 'remove the part where X happens', or "
                "deleting a mistake/interruption from the middle while keeping "
                "everything before and after it. Both 'start' and 'end' "
                "(seconds) are required and mark the range to DELETE. If what's "
                "actually wanted is dropping only the intro or only the tail "
                "(nothing survives on one side), use trim instead -- it's "
                "cheaper and simpler for that case."
            ),
            parameter_schema={"start": float, "end": float},
        ),
        "merge": StageCapability(
            name="merge",
            description=(
                "Join several videos end to end into a single clip, in the order "
                "listed in video_ids. Requires at least two videos, and must be "
                "the FIRST stage of the workflow -- later stages operate on one "
                "video, so a merge cannot be placed after other edits. To edit "
                "the joined result, put merge first and the other stages after "
                "it. Clips of different sizes are fitted onto the first clip's "
                "frame; clips without sound are joined with silence."
            ),
            parameter_schema={},
            must_be_first_stage=True,
        ),
        "transcribe": StageCapability(
            name="transcribe",
            description=(
                "Listen to the speech in the video and produce a text transcript "
                "plus a subtitle (.srt) file. Does not change the video itself. "
                "Use it whenever captions or a transcript are wanted -- and note "
                "it must come BEFORE 'burn_subtitles', which needs the subtitle "
                "file this produces. 'language' is an optional hint like 'en' or "
                "'es'; leave it out to detect the language automatically."
            ),
            parameter_schema={"language": str},
            produces_asset_kinds=("video", "transcript", "srt"),
        ),
        "burn_subtitles": StageCapability(
            name="burn_subtitles",
            description=(
                "Draw captions permanently into the picture, so they show without "
                "the viewer turning subtitles on -- which is what social video "
                "needs, since most of it is watched muted. Requires a 'transcribe' "
                "stage earlier in the workflow to supply the subtitle file. "
                "'style' is one of default, large, small."
            ),
            parameter_schema={"style": str},
            requires_asset_kinds=("video", "srt"),
        ),
        "render": StageCapability(
            name="render",
            description=(
                "Produce the final file in a chosen format, resolution and "
                "quality. 'format' is mp4 (plays everywhere, the safe default), "
                "mov, webm, or gif (silent, short loops). 'resolution' is a "
                "shorthand like '1080p' or '720p' -- width follows automatically "
                "so nothing is stretched. 'bitrate' like '5M' or '2500k' trades "
                "file size against quality. Usually the last stage, but it does "
                "not have to be: rendering a master and then trimming a short "
                "clip from it is perfectly valid."
            ),
            parameter_schema={"resolution": str, "format": str, "bitrate": str},
        ),
        # Phase 10's foundation-scoped capabilities (§19 Phase 10): the
        # two that need no new dependency (ffmpeg's own scene filter;
        # a regex scan over a transcript already being produced/cached).
        "detect_scenes": StageCapability(
            name="detect_scenes",
            description=(
                "Find shot/scene-cut timestamps in the video -- points where the "
                "picture changes abruptly, e.g. a camera cut or an edit point in "
                "the source footage. Does not change the video itself; produces a "
                "list of cut times for review or for choosing where to trim. "
                "'threshold' (0 to 1, default 0.4) is sensitivity: lower catches "
                "subtler cuts at the cost of false positives on fast pans."
            ),
            parameter_schema={"threshold": float},
            produces_asset_kinds=("video", "scenes"),
        ),
        "detect_filler_words": StageCapability(
            name="detect_filler_words",
            description=(
                "Flag verbal fillers already present in the transcript -- 'um', "
                "'uh', 'erm', 'hmm' -- with roughly where each occurs. Does not "
                "remove anything or change the video; produces a list for review. "
                "Requires a 'transcribe' stage earlier in the workflow to supply "
                "the transcript this reads."
            ),
            parameter_schema={},
            requires_asset_kinds=("video", "transcript"),
            produces_asset_kinds=("video", "filler_words"),
        ),
    }
)
