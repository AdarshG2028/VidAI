# Setu → AI Conversational Video Editor: Implementation Blueprint (v2)

This supersedes the v1 architecture document. Same investigation of the codebase underlies it; this version folds in your refinements and is structured as a phase-by-phase build plan rather than a pure architecture description. Where something is unchanged from v1 it's stated briefly with a pointer to why; new/changed material is expanded.

## Changelog from v1

- **Planner output is now a strict schema** — a list of `{stage, params}` objects, not a bare stage-name list plus a side-channel params dict. A new explicit **validation/translation layer** turns this into Setu's native `workflow: list[str]` + `payload` shape before it ever reaches `JobSubmissionService`. (§6)
- **Notification Worker removed for V1.** Frontend polls `GET /jobs/{id}`, which already exists and already returns everything needed. The terminal-lifecycle-event idea from v1 is demoted to an optional backlog item, not a V1 requirement. (§9, §10, §20)
- **Roadmap rewritten** as small, independently-demoable phases, each with Goal / Components / DB changes / API changes / Worker changes / Frontend changes / Testing strategy / Demo scenario / Risks. (§19)
- Everything else — the two-job split, memory design, storage-first sequencing, "reuse Setu unmodified" posture — is confirmed as-is from v1 and carried forward.

## Changelog from v2

- **Phase 5 (editing workers) is no longer one phase.** It's now five sequential sub-phases — Crop, Color, Audio, Subtitle, Export — each fully implemented, tested, and demoed before the next starts. The system stays in a working, demoable state after every single worker. (§19, Phase 5a–5e)
- **Memory updates are no longer triggered from `GET /jobs/{id}`.** GET endpoints stay read-only, no hidden side effects. The frontend now explicitly calls `POST /jobs/{id}/update-memory` once it observes a job has completed. The endpoint is idempotent. (§17, §19 Phase 6)

## Changelog from v3

- **Phases 8 and 9 added** (2026-07-24): Phase 8 — Collaborative Project Rooms (multi-user shared workspace, project-scoped conversations, WebSocket sync); Phase 9 — AI Collaboration & Proposal Workflow (multi-participant-aware planner, proposals, approval policies, task ownership + cancellation). (§19)
- **The "Setu core stays unmodified" rule is relaxed to a strong default.** Phases 0–7 treated it as hard; from Phase 8 on, Setu may be modified when there's a genuinely good reason, documented at the point of change. Phase 9b's cooperative cancellation is the first deliberate exception. (§12, §19 Phase 9b)
- **Cancellation moves out of the backlog** into Phase 9b. (§20)

## Changelog from v4

- **Phase 9 split into 9a and 9b**, since it had grown to bundle three separable concerns: planner facilitation + proposal data model, approval-policy evaluation, and a Setu-core change (cancellation). **9a — Proposals & Approval Workflow** (planner, proposals, approval policies) needs zero Setu changes and is independently demoable. **9b — Cooperative Cancellation** is the one justified Setu-core change, isolated on its own so it can be reviewed/tested/demoed in isolation from the approval mechanics. (§19)
- **Approval policies redefined as `admin` / `team`**, replacing the earlier `owner` / `majority` / `unanimous` draft. `team` (unanimous, every active member) is **the V1 default** — chosen over `admin` because it fits this product's small-creative-team framing, at the cost of the reject/re-discussion lifecycle it requires (below). `majority` moves to the backlog alongside newly-added future policies (Selected Reviewers, Two-Level Approval, Custom Approval Rules). The `admin` policy name also deliberately replaces `owner` to stop it colliding with **job ownership** (the separate, unrelated concept used for cancellation authorization) — same word, two different decision-makers, worth naming distinctly. (§19 Phase 9a)
- **Proposal rejection is no longer a dead end.** Under `team`, any single reject immediately ends the proposal (unanimity is already impossible — no need to wait out the rest of the vote) and returns the room to open discussion; the planner may then produce a revised proposal as a new row, with the rejected one kept as an audit record. (§19 Phase 9a)
- **The planner is now approval-policy-aware** — it renders the room's active policy in its prompt context and phrases facilitation messages accordingly (e.g. "ready for admin approval" vs "waiting for approval from all team members"). This is wording only: the planner still never decides approval outcomes or executes workflows, only produces proposals/messages — that stays the collaboration layer's job. (§19 Phase 9a)

## Changelog from v5

- **Project is now the aggregate root, effective immediately rather than at Phase 8.** `Project` was originally going to be introduced by Phase 8 (Collaborative Project Rooms); it actually landed when Phase 2 was implemented (`Conversation` was parented to `Project`, not `Video`, from the start — see the Phase 2 section) and was then extended to `Video` as a deliberate follow-up migration. Every video now belongs to exactly one project; there is no such thing as an orphan video. (§14, §19 Phase 1, Phase 8)
- **`POST /videos` is gone, replaced by `POST /projects/{id}/videos`.** `GET /videos/{id}` is unchanged (reading by a video's own id is still a reasonable flat lookup) but now returns `project_id`. A new `GET /projects/{id}/videos` lists a project's videos. (§19 Phase 1)
- **`videos.project_id` and `conversations.project_id` are both `NOT NULL`** (the latter also `UNIQUE` — one shared conversation per project). Phase 8's original text describing a *nullable* `project_id` gained "later" is corrected in place — nullable-then-backfilled was never how this shipped. `videos.project_id` also changed from `ON DELETE SET NULL` to `ON DELETE CASCADE`: deleting a project deletes its videos, matching "no orphan video" above. (§14, §19 Phase 8)
- **This confirms the intended product UI flow end to end**: create a project → upload video(s) into it → invite members (Phase 8) → members converse (Phase 2, then Phase 9a once proposals exist) → approved proposal executes the edit (Phase 3/4). Nothing here is a new phase — it's the mapping the phases below already implement.
- **Multiple videos per project already works** — `videos.project_id` was never unique, unlike `conversations.project_id`. What's *not* built yet: a user-entered display name per video, distinct from the uploaded file's name. Small, additive, deliberately deferred (see Phase 1's Risks).

## Changelog from v6

- **AI architecture principles added** (2026-07-26, before Phase 3 started): a new subsection under §5 sets explicit constraints for Phase 4 onward — single stateless planner (no multi-agent frameworks), ordinary services rather than an agent abstraction, no memory beyond §7's `user_preferences`, no premature conversation-summarization, general complexity discipline (stop and explain before adding orchestration/memory a phase seems to need). Documentation only — no code changed. (§5)
- **Stale `"plan"` terminology fixed throughout** — §5, §6, §13, Phase 2, Phase 3 (including its heading), Phase 4, and Phase 5a all still said `{"type": "plan", ...}` / "plan validator" / "a plan" even though the actual Phase 2 implementation (`StaticPlanner`) already ships `{"type": "proposal", ...}`, precisely to avoid a rename at Phase 9a. This was drift between the doc and shipped code dating back to Phase 2, not a new decision — now corrected to match what's actually running. Phase 9a's text describing the discriminator "evolving" from `plan` to `proposal` is also corrected: that shape was already `proposal` from Phase 2, so Phase 9a only adds two new fields (`reasoning`, `discussion_summary`) to the existing shape, not a rename.

## Changelog from v7

- **Phase 3's design revised before any code was written** (2026-07-27), for long-term extensibility across the AI planner, multiple editing stages, collaboration, and multiple assets — while keeping Phase 3 itself simple, per the AI architecture principles (§5). "Translator" renamed **"compiler"** throughout (§6, Phase 3, Phase 4, Phase 9a) — its future responsibilities (asset resolution, param normalization, default injection, shorthand expansion) are closer to compilation than plain translation; stays a pure function regardless of name. The module-level `CAPABILITY_REGISTRY` constant becomes a small `CapabilityRegistry` class (`get`/`exists`/`list`/`register`) so the validator depends on an interface, not a global — still backed by a hardcoded dict for V1. Raw dicts inside the validation/compilation pipeline become `Proposal`/`ProposalStage` frozen dataclasses (matching `StageMessage`'s existing convention), explicitly **not** touching `planner.py`/`ConversationService`, which still pass plain dicts until Phase 4 does that conversion for real. The validator returns a `ValidationResult` (collecting every error, not just the first) instead of only raising, since Phase 4's regenerate-and-retry loop needs the full error set to feed back into the LLM prompt. `video_uri` moves into a one-field `ExecutionContext` dataclass alongside the `Proposal`, keeping the compiler's signature stable as more context needs to travel with it later. Duplicate stage names in one proposal are rejected by the validator (a deliberate V1 policy, not a Setu constraint — Setu's engine would mechanically tolerate them). (§19 Phase 3)
- **`StageCapability` deliberately does not gain speculative fields.** `parameter_schema` (renamed from `params`) and `description` are kept; `worker_name`, `category`, `version`, `supports_batch` were proposed but declined for now — `worker_name` would duplicate the `stage name = topic name = WORKERS key` identity §6 already establishes, and the other three have no consumer anywhere in Phase 4 or Phase 5's plan as written. Each gets added in whichever sub-phase first actually needs it. (§19 Phase 3)

## Changelog from v8

Architectural refinement pass before Phase 4 implementation started (2026-07-27), user-directed, documentation-first per the same discipline as v6/v7 — no Phase 4 code existed yet when this was written.

- **`Planner`'s interface signature changes** — this is the largest single deviation from a literal reading of the refinement brief, and it's a *consequence* of two of the brief's own items, not an independent choice: item 3 introduces a `PlannerContext` model specifically to replace "many unrelated arguments" into the planner, which is incompatible with item 1's `respond(messages, preferences)` shape surviving unchanged. `Planner.respond` becomes `respond(context: PlannerContext) -> PlannerResponse` — both the input *and* the output are now typed domain objects, not `dict[str, Any]`, per item 4's "avoid passing raw dictionaries throughout the application" (a `dict` return would have been a half-measure of the same principle). `StaticPlanner` moves to the new signature too, since it implements the same interface; its Phase 2 tests move with it. `ConversationService` is the only caller and absorbs the new context-assembly + serialization responsibility (see below) — this is exactly the seam Phase 2's own docstring anticipated ("Phase 4 swaps StaticPlanner for a real LLMPlanner ... nothing about this sequencing changes"), it just turned out the *signature* changes even though the *sequencing* doesn't.
- **New services introduced, each a plain stateless function/class per the v6 principles — not an agent abstraction:**
  - `PlannerContext` (`backend/services/planner_context.py`) — one dataclass carrying everything a planning call needs: project, conversation history, the project's videos, user preferences, the capability registry, and an `approval_policy` field that's a placeholder today (unused until Phase 9a) so that phase doesn't need to touch the planner's context shape again.
  - `PromptBuilder` (`backend/services/prompt_builder.py`) — `PlannerContext → Prompt`, extracted so future prompt types (clarification, revision, summarization) can reuse the same construction step without duplicating it inside `LLMPlanner`.
  - `PlannerResponse` (alongside `Proposal`/`ProposalStage` conventions from Phase 3) — `type` / `message` / `proposal`, where `proposal` is exactly Phase 3's `Proposal` (no new proposal shape).
  - `LLMClient` (`backend/services/llm_client.py`) — a small abstraction so `LLMPlanner` never imports a provider SDK directly. **Only one concrete implementation ships now: `GroqClient`.** Stub classes for other providers (OpenAI, Anthropic, etc.) are explicitly *not* pre-built — per the v6 "no speculative generality" discipline, an interface plus its one real caller is enough until a second provider is actually being integrated.
- **Groq is the V1 provider**, using its OpenAI-compatible `response_format={"type": "json_schema", ...}` mode (verified against the installed `groq` SDK — real schema-enforced structured output on supported models, not prompt-engineered JSON; §item 6's requirement holds). Model name is a `Settings` field, not hardcoded, so switching within Groq's supported-models list needs no code change.
- **Retry strategy is two-layered and bounded.** Infrastructure retry covers network failures, provider timeouts, and malformed (non-parseable) structured output. Semantic retry is separate and stays to *exactly one* regeneration: if `validate_proposal` (Phase 3, untouched — see below) rejects the LLM's proposal, the planner regenerates once with the `ValidationResult` errors fed back into the prompt; if that regeneration still fails validation, the user gets a friendly clarification message instead of a job. No recursive retries, no reflection/self-critique loop, no unbounded regeneration — this is the literal gate v6 principle 1 calls for at Phase 4, applied.
- **`validate_proposal` and `compile_workflow` are reused as-is where possible; `compile_workflow`'s input/output shapes change, its responsibility doesn't.** Phase 3's validator logic (unknown-stage / unknown-param / type-mismatch / duplicate-stage checks) is untouched code. What changes, and why (see Multiple Videos below): `ExecutionContext.video_uri: str` → `ExecutionContext.video_uris: dict[str, str]`, and `ProposalStage` gains `video_ids: list[str]`. This is a data-shape change, not a new compiler responsibility — `compile_workflow` still does only `Proposal → Workflow`, it just resolves per-stage video handles into URIs while doing it, which stays a pure dict lookup rather than a general asset-resolution system.
- **Multiple videos are in scope for Phase 4, reversing an earlier draft of this same refinement pass that said to keep a single target video until Phase 5.** The reversal is deliberately narrow: a project already holds many `Video` rows (no unique constraint, since Phase 1/2) so the planner already needs to choose among several when building a proposal — Phase 4 makes that explicit at the model level rather than pretending it isn't true yet. `ProposalStage.video_ids` uses short LLM-facing handles (e.g. `"video_1"`, not raw `Video.id` UUIDs — models are unreliable at echoing UUIDs verbatim) that `PromptBuilder` assigns from `PlannerContext.videos` and `ConversationService` resolves back to real storage URIs when building `ExecutionContext`. This is **not** the generic `Asset`/`AssetRegistry`/`AssetResolution` system deferred below — it's a video-specific id→uri dict, already known data, no resolver abstraction.
- **Known landmine, deliberately not fixed in Phase 4:** `validate_proposal`'s duplicate-stage-name rejection (`backend/services/proposal_validator.py`, keyed only on `item.stage`) will reject a legitimate future workflow like `crop(video_1) → crop(video_2) → combine(video_1, video_2)`, since `"crop"` appears twice regardless of differing `video_ids`. This can't actually trigger in Phase 4 (the registry only has `"dummy"` registered — no `crop`/`combine` exist yet), so item 7's "don't change the Phase 3 validator" holds for this phase. Phase 5, when it introduces per-stage workers, will need to change the uniqueness key from `stage name` to `(stage name, video_ids)`. Recorded here so it's a planned Phase 5 change, not a surprise.
- **`POST /projects/{id}/confirm-proposal`** is the new, explicit confirmation entry point — free-text chat confirmation is deliberately not supported. It scans the conversation backwards for the most recent `{"type": "proposal"}` message (not just the last message — a clarifying turn can follow a proposal), compiles it, and submits via the existing `JobSubmissionService.submit()`. **Idempotency reuses Setu's existing idempotency-key mechanism (Phase 1) rather than adding a new persisted "already confirmed" flag** — the key is derived deterministically from the conversation id and the proposal message's id, so calling the endpoint twice replays the same `Job` instead of creating a second one. This satisfies the "no proposal persistence" deferral below: nothing new is stored, and the compiled `(workflow, payload)` must remain a pure function of the proposal + video rows (no fresh timestamps or regenerated URIs) for the idempotency-key hash to match on replay.
- **Observability is additive logging only** around planner execution (provider, model, latency, prompt/completion tokens, whether regeneration was used, validation success/failure) — no tracing/metrics redesign.
- **Deferred to Phase 5** (explicitly, so Phase 4 doesn't reach for them early): richer `StageCapability` fields (`category`, `version`, `supports_batch`, execution hints) beyond what the planner already needs; any `compile_workflow` responsibility beyond `Proposal → Workflow` (param normalization, macro expansion, workflow optimization); the generic `Asset`/`AssetRegistry`/`AssetResolution` system (Phase 4 keeps using `Video` rows directly, just referenced by more than one per proposal now); redesigning worker/capability metadata ahead of Crop/Color/Audio/Subtitle/Export actually landing.
- **Deferred to Phase 8/9**: proposal persistence, proposal version history, proposal revisions, a real approval engine, collaborative proposal editing, proposal ownership, multiple proposal versions, and any `proposals`-table-shaped DB model beyond what Phase 4 needs (which is none — see confirm-proposal above).
- **General posture note, generalizing the v3/v4 changelog's "Setu core unmodified is a strong default, not absolute, from Phase 8 on":** that posture applies at *any* phase when there's a genuinely good reason, including Phase 4, despite Phase 4's own stated philosophy of "nothing in Setu's execution engine changes." In this pass specifically, no Setu-core change was needed — `Job.payload` is an opaque `dict[str, Any]` never inspected by `JobSubmissionService`/`StageProcessingService`/the workflow engine, so the `ExecutionContext`/`ProposalStage` reshape above is entirely contained to this project's own Phase-3-owned files. Recorded as a posture clarification, not because this pass needed to exercise it.

## Changelog from v9

Prompted by a user question (2026-07-30, after Phase 4's implementation was manually live-tested): since Phases 8 and 9 were bolted on (2026-07-24) after Phases 5–7 were already planned, should the resulting Phase 5→9b sequence be restructured for efficiency? A review of every phase's stated "Why after Phase X" dependency found Phases 5→6→7 and 8→9a both genuine and load-bearing — no change needed there. One dependency was artificial:

- **Phase 9b's dependency on 9a is corrected: it only ever needed job ownership, not the approval/proposal machinery.** `project_jobs` (Phase 8) gains a `submitted_by_user_id` column, captured at submission time regardless of whether that submission came from Phase 4/8's plain `confirm-proposal` or Phase 9a's approval-triggered path. Phase 9b's authorization check now reads this column directly, not `proposals.created_by_user_id` — so 9b's true prerequisite is Phase 8, not 9a. It stays listed after 9a in the roadmap only because both are grouped as the "Phase 9" collaboration-and-execution-control bundle for review purposes (the same reasoning the v4 changelog gave for splitting 9 into 9a/9b), not because of a hard technical dependency. (§19 Phase 8, Phase 9a, Phase 9b)
- **Deliberately left unchanged:** the macro order 5→6→7→8→9a→9b — a targeted fix was chosen over a broader reorder. Also deliberately not pulled earlier: Phase 8 itself, despite its real technical prerequisites (project-scoping, sender-attributed messages) landing as early as Phase 1/2 — building collaboration on top of the `dummy` stage with no memory loop or frontend would be a weaker demo, and that tradeoff wasn't what was asked for here.

## Changelog from v10

Phase 5 architecture review (2026-07-31), user-directed, before any Phase 5 code was written. Phase 5 is rewritten; Phases 7, 9a, 10 and §20 are updated in consequence.

- **Phase 5 restructured around capabilities on an asset model**, replacing the five-worker Crop/Color/Audio/Subtitle/Export plan with seven sub-phases (5A infrastructure, 5B spatial transforms, 5C adjustments, 5D audio, 5E temporal/structural, 5F transcript/subtitles, 5G render). Stages now produce **typed assets** (`video`/`transcript`/`srt`/…) rather than an implicit single video — necessary because transcription produces no video, and no single `output_uri` key extends to that. (§19 Phase 5)
- **"Capabilities not workers" required no restructuring — it was already the architecture.** The planner only ever sees `CapabilityRegistry`/`StageCapability` and never imports `backend.workers`; `Worker` already is the internal implementation a capability is expressed through. Recorded because the question was asked and the answer is "already satisfied," not "deferred."
- **Chaining stays worker-side via `previous_output`, deliberately not moved into `WorkflowEngine`.** The engine documents a real invariant (never imports from `backend/workers/`, treats `message.payload` as opaque); `previous_output` is Setu's existing purpose-built mechanism for stage-to-stage handoff. Implemented as **monotonic asset accumulation**, which also removes any adjacency requirement between a producing and consuming stage. Note this was evaluated on its merits, *not* out of deference to the "don't modify Setu" rule — that rule is treated as a soft default per the v9 posture, and this phase does make one deliberate core change (below).
- **One deliberate Setu-core change: `Result.artifact_uri` is finally populated**, from the primary video asset, in `StageProcessingService._record_success`. The column has existed unused since it was added; this completes a mechanism the schema already anticipated. Purely additive.
- **`trim` added — the most consequential single addition in this pass.** No earlier draft of Phase 5, in this document *or* in the review request that prompted it, contained any way to cut a video. Without it the product can enhance video but cannot edit it; "cut out the boring middle" is the most common editing operation there is. One input, one output, no engine change. Time-range params become a cross-cutting convention on other capabilities for the same reason. (§19 Phase 5E)
- **`merge` added, first-stage only** — free under the existing model, since `compile_workflow` already builds `video_uris` as a list and stage 0's handles are real. **`split` and mid-chain merge are explicitly deferred** to §20 item 6 with their two concrete blockers named (`Result`'s per-stage uniqueness constraint; `previous_output` sourcing stage N-1 only) — a real execution-model change, not a capability.
- **The duplicate-stage-name check is dropped rather than re-keyed**, reversing v8's planned fix. Asset chaining makes a repeated stage meaningful (`trim → trim` is an ordinary "drop the intro, then the boring middle" workflow), and the proposed `(stage, video_ids)` key wouldn't have worked anyway since only stage 0's handles are real. Corrected in Phase 4's Risks and resolved in Phase 5A.
- **Transcription split from subtitle burn-in** (`transcribe` / `burn_subtitles`), making the `.srt` a first-class downloadable artifact rather than only burned-in pixels. `validate_proposal` gains a forward-scan asset-availability check so a proposal consuming an asset nothing produces fails *validation* — feeding Phase 4's existing semantic-retry loop — instead of failing at runtime into the DLQ, which is the exact failure class fixed in Phase 4 for video handles.
- **Overlay and Blur reviewed and deliberately not added.** Under a correct asset model they cost the same later as now, so front-loading them buys risk (new dependencies, new failure modes) with no capability gain, and there is no frontend until Phase 7 to show them in.
- **Artifact retrieval endpoints pulled into Phase 5A** (`GET /jobs/{id}/artifacts` + byte-serving download). Discovered during review: **no endpoint in the API can serve file bytes at all** — storage URIs are opaque `local://…` strings — so without this there is literally no way to retrieve output, and no way to verify Phase 5's own capabilities except by inspecting the filesystem by hand. Phase 7 already assumed this existed.
- **Preview mode added (5A), and Phase 7 gains the iteration loop.** A preview is not a new stage — it's the same workflow compiled in a different mode and submitted as an ordinary Job, so it inherits retry/DLQ/idempotency/observability for free; the flag is honored in exactly one place (`run_ffmpeg`). Phase 7's flow becomes *propose → preview → adjust → confirm*, with per-stage artifact inspection for diagnosing which step went wrong. Phase 7's "download the completed video" is corrected to **artifacts, plural**. (§19 Phase 5A, Phase 7)
- **Phase 10 (semantic video understanding) sketched into the roadmap** — previously referenced only in conversation, absent from this document. Its one structural requirement is recorded now: **assets become video-scoped rather than job-scoped** (a `video_assets` table promoting Phase 5A's `Asset` from per-run to durable per-video), which is cheap precisely because 5A defines `Asset` as a clean value object. Its transcript-caching half is flagged as worth pulling forward early, since transcription is the one preview cost that resolution reduction can't lower. (§19 Phase 10)
- **Phase 9a's `team` default flagged as an open question**, not changed: unanimous approval fits a final render but may grind against Phase 5's iteration loop. Two candidate resolutions recorded; to be decided with the preview loop in hand rather than speculatively. (§19 Phase 9a)
- **§20 gains four items:** multi-input/multi-output execution (item 6, the only backlog item requiring an execution-model change), partial re-run / resume-from-stage, artifact retention/GC, and capability filtering at scale (~30+ capabilities degrade prompt quality; the fix is retrieval, explicitly not multi-agent).
- **Terminology unchanged.** The review used `"stages"`/`"parameters"`; the codebase uses `"workflow"`/`"params"`. Renaming would touch `Proposal.from_dict`, the planner response schema, the prompt, and every existing test for zero behavior change — and this document has already paid for one terminology drift (v6). Keeping existing keys.

## Changelog from v11

- **Phase 5F's transcription moves from local `faster-whisper` to Groq's hosted `whisper-large-v3`** (2026-07-31, before 5F was implemented). The same model weights either way, so v10's "no compromise on transcription quality" decision stands unchanged — this only moves where the inference runs. Verified before deciding: `groq>=1.6.0` is already a dependency for the planner, and its SDK already exposes `audio.transcriptions.create` with `timestamp_granularities`, so **5F now adds no new Python dependency at all** (v10 had planned one).
- **The deployment target shrinks as a direct result.** Whisper was the only component requiring ~3GB of model on disk and 1.5-3GB resident RAM, and the sole reason a single-box deployment needed ~8GB; a 4GB instance is now comfortable. It also loses its status as "the sub-phase most likely to run long", since CPU-bound transcription at roughly 1-2x realtime was what earned it that label.
- **The cost is a network dependency at job time**, recorded rather than glossed: not a new failure domain (planning already calls Groq), and it maps onto the existing taxonomy without additions — timeouts retry, rejected files DLQ. Groq's ~25MB upload cap is handled by sending extracted 16kHz mono audio (~14MB/hour) instead of the video. (§9, §19 Phase 5F, §19 Phase 10)

## Changelog from v12

- **Object storage promoted out of the backlog into Phase 5H** (2026-07-31, user-directed), targeting **Cloudflare R2**. Chosen for egress rather than storage cost — R2 charges nothing to serve, S3 roughly $0.09/GB, and serving video is where that compounds. Since R2 speaks the S3 API, `boto3` covers R2, B2 and MinIO equally, so this is a provider choice rather than a lock-in.
- **Sequenced before Phase 6/7, not after.** The trigger is not that single-host storage has failed — it hasn't. It is that Phase 7 builds its download/preview UI against `GET /artifacts`, and this phase changes that endpoint from streaming bytes to redirecting to a presigned URL. Doing it after would mean reworking freshly-written frontend code. It also keeps migration free: with no real users the bucket can simply start empty, which stops being true once uploads are real.
- **Local disk is not removed.** `STORAGE_BACKEND` selects it, and it stays the default for tests and no-credentials checkouts.
- **Recorded as the phase's main risk:** `GET /artifacts` delegates path-traversal defence to the storage backend rather than duplicating it, so `S3Storage` must implement its own key validation. `uri` is attacker-controlled; a backend that passes it straight through reopens the hole `LocalDiskStorage._path_for` closes. (§19 Phase 5H, §20 item 11)

## Changelog from v13

Written during Phase 8's implementation (2026-08-01), recording decisions the phase text did not anticipate.

- **`GET /artifacts` is authorized, and its URL shape changed** to `/artifacts?uri=…&job_id=…`. Phase 5A's description of it — "a byte-serving download route validated through `LocalDiskStorage`'s existing path guard" — described only traversal defence; there was no *authorization* at all, so anyone able to name a URI could fetch its bytes, and URIs travel (every artifact listing and every room snapshot hands them out, and they outlive the membership of whoever saw them). The guard is `require_artifact_access` in `backend/api/deps.py`, checking both that the job belongs to a room the caller is in **and** that the job actually produced that URI. (§19 Phase 5A, Phase 8)
- **Authorization takes a job because URI → project is ill-posed.** Not a convenience: assets forward monotonically (`media.forward_assets`), so a preview and the confirm job after it reference the same object by construction, and every stage after a `transcribe` re-reports a video it never touched. "Which room owns this URI" has no single answer; "is this job in a room you are in, and does it contain this URI" does. The route stays *flat* rather than moving under `/jobs/{id}` — the URI is still the address, the job is only the authorization context. **A job with no `project_jobs` row is refused outright** (raw `POST /jobs` submissions, which are product-internal); previously unmapped meant world-readable. (§19 Phase 8)
- **Identity may travel as a `user_id` query parameter, not only the `X-User-Id` header.** A browser cannot attach headers to `<video src="/artifacts?…">` — the very client the download route streams and honours Range for — nor to `new WebSocket(url)`, which this phase's membership-checked socket handshake needs. Header-only identity would have made both unusable. There is no security delta: `X-User-Id` is asserted by the client and believed, so a query parameter is exactly as forgeable. It is more *visible* (query strings reach access logs and history), which is a reason this transport must not be carried over verbatim when real auth replaces it. The header remains the default and wins when both are sent. (§19 Phase 8)
- **The room snapshot returns members**, a fifth section beyond the four this phase's API text enumerates. The socket emits `member.joined`, and forcing a client to refetch members separately after every reconnect recreates the multi-request problem the snapshot exists to remove.
- **What counts as an export is a product rule, stated here because Phase 9a and the socket both inherit it:** a completed job that is *not a preview* and left at least one asset behind. Previews are excluded on the `_preview` payload flag, since a preview is deliberately the same workflow at low resolution and a throwaway 480p render must not enter the version list; `video_analysis` and `dummy` fall out on their own by producing nothing, so no worker allowlist is hardcoded. Exports carry only the final stage's assets — `artifact_cleanup_service` sweeps intermediates after the retention window, and per-stage inspection already has `GET /jobs/{id}/artifacts`. `active_jobs` is `not in JobStatus.terminal()`, so a retryable failure still reads as active. (§19 Phase 8)
- **Known gap, deliberate:** `GET /jobs/{id}` and `GET /jobs/{id}/artifacts` stay unauthenticated, so a job UUID still discloses its URI *list*, though no longer its bytes. Those are Setu's generic API and the semantics for unmapped jobs there are unresolved; closing it is a separate decision, not a Phase 8 deliverable.

## Changelog from v14

**Phase 8 is complete** (2026-08-01). All four room endpoints, the socket, and all five event types shipped. These record what the socket's implementation settled that the phase text left open.

- **`seq` lives on the bus, per room — not per connection.** A per-connection counter looks identical in tests and is useless for the thing `seq` exists for. `GET /projects/{id}` reports the same counter the socket advances, which is what makes "reconnect, refetch the snapshot, resume" a comparison between two numbers from one source. It advances even with nobody connected and survives the last subscriber leaving, or a returning client would see numbers it had already used and read every reconnect as a gap. It **resets when the process restarts**, which is harmless precisely because reconnect always re-baselines against a fresh snapshot — an epoch would be ceremony protecting nothing. The handoff race is closed client-side: connect first, snapshot second, discard events at or below `snapshot.seq`.
- **Events are published from services after `commit()`, not from routes.** Emitting before the commit lets a client be told about a message, refetch the snapshot, and not find it — manufacturing exactly the gap `seq` exists to report. "The socket is pure fan-out / REST is the only write path" constrains what arrives *over* the socket, not where emission happens.
- **Backpressure closes the connection rather than dropping events.** A client that stops reading has its oldest event discarded to make room for a close sentinel and is sent down the documented recovery path (close code 1013, distinct from an ordinary shutdown's 1001). Silently trimming would leave it quietly wrong until it happened to notice a gap.
- **The handshake cannot reuse the membership dependency.** `require_project_member` raises `HTTPException`, and there is no response to put a status on once a handshake is under way. Membership is checked explicitly and refused with close code 1008 — **byte-identical** for a non-member and a nonexistent room, for the same reason that guard returns 404 rather than 403. Identity arrives as the `user_id` query parameter (Changelog v13), since a browser cannot set headers on `new WebSocket(url)`.
- **The progress poller is gated on listeners, not on running jobs.** It polls only rooms that currently have a socket open: with nobody listening there is nothing to send, so scanning the jobs table would be work performed for no observer. It diffs rather than re-sends, does not replay a job that was already finished when a client connected (the snapshot just handed that client the same thing), and drops a room's baseline entirely when its last listener leaves — without which the first tick after a reconnect re-announces an export the snapshot had just listed. Bounded by `list_jobs_for_project`'s limit, so a room with more jobs than that would stop reporting progress on its oldest still-running one; the fix when it matters is to fetch the room's *active* jobs there.
- **`export.completed` and the snapshot share one predicate** (`export_artifacts`, in `room_snapshot_service.py`), and the event builds its download URLs through `ArtifactResponse`. A stream announcing a set of exports that a reconnect then disagrees with would be worse than emitting no event at all, and the URL format has already changed once (v13) and changes again at Phase 5H.

## Changelog from v15

**Phase 9a and Phase 9b are both complete** (2026-08-01). 9b shipped first — the roadmap's own text confirms its only real prerequisite is `project_jobs.submitted_by_user_id` (Phase 8), not the approval machinery — then 9a's three steps followed. These record what implementation settled, including the one open question Phase 9a's own text deliberately left unresolved.

- **The `team`-default open question (§19 Phase 9a, "may be real friction once Phase 5's preview/iteration loop exists") is resolved: previews are exempt from the approval policy entirely.** Of the two candidate resolutions the text offered — default to `admin`, or scope the policy per action — the second was taken, and it turns out **not** to add the extra dimension to `is_satisfied`/`is_rejected` the text worried it would: those two functions stay exactly the 3-argument shape already specified, because a preview simply never reaches them. `POST /projects/{id}/preview-proposal` calls straight into compilation, ungoverned; only `POST /proposals/{id}/approve|reject` touches policy. `team` stays the V1 default.
- **`POST /projects/{id}/confirm-proposal` is removed, not merely superseded.** §19's original API list assumed both it and 9a's approval endpoints would coexist. Once a real policy exists, a surviving direct-submit endpoint would let any member bypass it and spend the room's compute on their own say-so — approval would be opt-in rather than the rule. Its replacement is `POST /proposals/{id}/approve`, which shares its compilation body with preview (`ProposalConfirmationService.submit`) so the two provably cannot diverge in how a proposal is compiled.
- **A project-settings endpoint was missing from the original design and is added: `PATCH /projects/{id}`, owner-only, currently setting only `approval_policy`.** Without it `admin` was unreachable through the API at all — `team` is the migration default and nothing wrote to the column afterward, so every room was permanently unanimous regardless of what an owner wanted. A policy change takes effect for whatever votes happen next; a proposal already sitting with partial votes is not grandfathered into the rule that was active when it was created.
- **Job ownership on an approval-triggered submission is the proposal's `created_by_user_id`, confirmed distinct from whoever cast the deciding vote** — §19's text already specified this (line 781), but it is easy to get backwards in the transactional plumbing, since the caller of `/approve` is naturally most present in that code path. Phase 9b authorizes cancellation directly against `project_jobs.submitted_by_user_id`, so the wrong value there would hand cancel rights to the wrong person.
- **Approval submission is three phases, and the middle one commits on its own, by necessity rather than convenience.** A conditional `UPDATE ... WHERE status = 'pending'` claims the `pending → approved` transition, so two members approving simultaneously cannot both submit and bill the room twice. `JobSubmissionService.submit()` then commits its own Job/Outbox/IdempotencyKey trio — reaching in to make that atomic with the claim would mean modifying Setu's core, which 9a (unlike 9b) deliberately does not do. A crash between submitting and recording the `job_id` leaves a proposal `approved` with none: visibly incomplete rather than silently wrong, and recoverable, since the idempotency key derives from the proposal's own id and a retry replays the same job.
- **Votes are filtered to current active membership on both sides of the arithmetic** (`ProjectMemberRepository.active_user_ids`/`count_active`), not just counted as cast. An outstanding invitation must not make `team` unanimity unreachable; a member who has since left must not hold a decision hostage by an uncounted stale vote.
- **9b's status check reads the database fresh, never the `Job` object loaded at the start of stage processing.** That object can be minutes stale by the time a long render's worker returns, and noticing a cancellation that arrived *during* the work — not just before it started — is the entire point of cooperative cancellation. Depends on the connection's isolation level being READ COMMITTED (verified); under REPEATABLE READ the fresh read would silently see the pre-stage snapshot and cancellation would appear to do nothing on exactly the long jobs it exists for.
- **The room snapshot gained a fifth job list, `ended_jobs` (cancelled or dead-lettered), beyond what Phase 8's text specified.** Both states are terminal, so they left `active_jobs`; neither produces an export. Together that meant a job a member had just cancelled disappeared from the room on the next refresh — tolerable when only a worker exhausting retries could reach those states, not once it is the visible result of a button Phase 9b adds.
- **The planner's transcript is attributed** (`member_1 (10:41): ...`, stable numbered handles assigned in order of first appearance — the same reasoning `video_1` handles got in Phase 4, since there is no users table and models echo raw UUIDs unreliably) **and facilitation instructions render only once a room has more than one participant.** `PlannerContext.approval_policy`, a Phase 4 placeholder never populated until now, carries the room's real policy so the planner can describe what happens next without promising a render that still needs votes — wording only; it never evaluates the policy itself.

## Changelog from v16

**Phase 10's foundation is complete** (2026-08-01): the `video_assets` table and per-video transcript caching, scoped deliberately to just those two things — see §19 Phase 10's own framing of this as "worth pulling forward ahead of the rest of Phase 10." The AI-understanding capabilities it lists (scene detection, diarization, face/object detection, saliency, embeddings) remain unbuilt. Unrelated: R2 (Phase 5H) was cancelled and then shipped against Amazon S3 instead, same day (2026-08-02) — see the v18 changelog entry and that section's header.

- **`video_assets(id, video_id, kind, uri, data, created_at, updated_at)` matches §19's sketch exactly**, keyed by `UNIQUE(video_id, kind)` — one *current* asset per kind per video, not a history (`Result` already keeps job-scoped history; this is a cache). Written and read through `VideoAssetRepository`, whose `upsert()` uses `ON CONFLICT DO UPDATE` rather than get-then-update, since two previews of the same video racing to cache the same transcript is a normal concurrent case, not an error.
- **`latest_analysis_job_id`'s pointer-chasing (Video → Job → Result stage 0) is deliberately left alone.** Migrating video_analysis's output onto this same table is a real follow-up but a separate, larger change (three call sites, a backfill decision) — not required for transcript caching, so not pulled in now.
- **The one structural gap this exposed: `Job` carries no `video_id`, and a stage's compiled `video_uris` are bytes only, positionally tied to a proposal's per-stage handles (`item.video_ids`, e.g. `"video_1"`) that never reach the worker.** A worker cannot resolve "which Video row is this" from a `StageMessage` alone. Fixed by threading a parallel list the same way `video_uris` already is: `ExecutionContext` gained `video_db_ids: dict[str, str]` (handle → `Video.id`, defaults to `{}`), `compile_workflow` emits it into `stage_params[i]["video_ids"]` alongside `video_uris`, and `media.py` gained `stage_video_ids()` mirroring `stage_video_uris()` — including the same "tolerate absence" contract (`[]`, not a `KeyError`), since not every `stage_params` producer is guaranteed to supply it.
- **Workers cannot hold a database session, which decided the caching design.** `WorkerRunner` constructs each `Worker` once at process startup and reuses it for every message; `StageProcessingService` (not the worker) gets a fresh session per message. So `TranscribeWorker` cannot query `video_assets` directly — it takes an injected `TranscriptCache` collaborator (mirroring the existing injected `TranscriptionClient`), whose default implementation (`SqlTranscriptCache`) opens its own short-lived session via `get_sessionmaker()` per call, the same "ambient accessor" shape as `get_storage()`/`get_settings()`. `StageProcessingService` stays exactly as ignorant of transcripts as it was of every other product concept; no hook was added to the `Worker` ABC. (Generalized to `VideoAssetCache`/`SqlVideoAssetCache` in v17 once a second capability needed the identical shape — see that changelog; the design reasoning here is unchanged, only the class names moved to `backend/workers/video_asset_cache.py`.)
- **The cache key is derived, not carried through the asset chain, and is deliberately conservative: valid only when nothing upstream produced a video asset yet** (`primary_video(previous_assets(previous_output)) is None`) **and no `language` override was given.** A trim or crop ahead of `transcribe` changes the audio a viewer actually hears, so reusing the original upload's transcript there would be a correctness bug, not a convenience — the gate exists specifically to keep the cache scoped to "reading the pristine upload," which is the one case the roadmap's stated payoff ("transcribe once, reuse across previews, re-runs, and final renders") actually describes.
- **A cache hit and a fresh transcription assemble their result through the same shared function (`_finish`)**, not two independently-written return statements — the early-return path would otherwise skip the "insert a VIDEO asset when nothing was carried" rule real transcription applies, silently dropping the video from what reaches the next stage on every cache hit. Caught by a dedicated test before the happy-path tests were written, not after.
- **A cache write failure is logged and swallowed, never raised** — a side-effect of a successful transcription must not dead-letter the job that produced it; the cost of a lost write is one avoidable re-transcription later, not a failed render now.

## Changelog from v17

**Two of Phase 10's six sketched capabilities shipped** (2026-08-02): `detect_scenes` and `detect_filler_words`, chosen specifically because they need **zero new dependencies** — every other item on the list (diarization, object/face detection, saliency) needs either a paid API or a heavy local model (`torch`, `pyannote.audio`), which would reverse the deployment-size decision Phase 5F already made deliberately (Groq-hosted Whisper over local `faster-whisper`, 8GB → 4GB). Scoped via AskUserQuestion rather than assumed, same as the v16 foundation work.

- **`detect_scenes` uses ffmpeg's own scene filter** (`select='gt(scene,X)'` + `showinfo`, parsed off the stderr `run_ffmpeg` already returns — the same mechanism the burn_subtitles luma-diff test already relies on), not a new scene-detection library. Produces a `scenes` asset (`{"threshold": ..., "cuts": [{"time": ...}]}`) and forwards the video untouched, same shape as `transcribe`. **A single continuous shot legitimately produces an empty `cuts` list — this is not an error and nothing raises on it**, a distinction worth stating because `transcribe`'s "no speech found → DLQ" precedent would otherwise pull an implementer toward raising on the empty case.
- **`detect_filler_words` runs entirely on the transcript `transcribe` already produces** — a regex scan (`\b(um+|uh+|erm+|hmm+|mhm+)\b`, case-insensitive), no ffmpeg pass, no API call. **Verified live before writing any detector code, not assumed**: Whisper models are well known to sometimes silently clean disfluencies out of a transcript, which would make a filler detector permanently find nothing. Checked with a Windows-TTS-synthesized clip saying "Well, um, I think, uh, we should..." through the real Groq `whisper-large-v3` integration — it transcribed "um"/"uh" verbatim, confirming the capability is viable before any implementation time was spent on it.
- **Deliberately narrow pattern list — "like", "so", "actually" are excluded.** Those are filler maybe a third of the time; flagging them indiscriminately reads as broken in exactly the demo this capability exists for. Semantic filler detection (distinguishing "I like it" from filler "like") needs an LLM pass — a different, larger feature, not attempted here.
- **`detect_filler_words` is deliberately NOT cached**, unlike its two siblings — for two independent reasons, either sufficient alone. Its gate can't work: it requires a `transcript` asset, so it can never run at stage 0, and the shared cache gate (`primary_video(previous_assets(...)) is None`) would always decline — caching it would ship dead code. And it doesn't need one: a regex scan over text already in hand costs microseconds, unlike `transcribe`'s Groq round-trip or `detect_scenes`'s ffmpeg pass.
- **The transcript cache from v16 was generalized from transcript-specific to `VideoAssetCache`/`SqlVideoAssetCache` (`backend/workers/video_asset_cache.py`) the moment a second capability (`detect_scenes`) needed the identical shape** — decided *before* writing `detect_scenes`, not refactored after, specifically to avoid two near-identical cache classes. `get`/`put` are now `(video_id, kind)`-keyed rather than a hardcoded transcript+srt pair; `TranscribeWorker` calls it twice per read/write instead of once. **Both-or-neither is preserved explicitly**: a transcript cached with no matching srt (an interrupted write) must fall through to real transcription, not return a half-populated asset set — the srt lookup only runs once the transcript lookup already hit, and a dedicated test (`test_cache_partial_hit_falls_back_to_real_transcription`) covers exactly this, since the refactor is the one place this specific regression could have been introduced silently.
- **A real, if minor, bug found and fixed in the v16 cache during this pass**: `SqlTranscriptCache.get()` (now `SqlVideoAssetCache.get()`) had no error handling, while `put()` did — a database blip on the *read* path would have dead-lettered a transcribe stage that a real transcription would have completed successfully, the exact failure mode the write path was already guarded against. Fixed and covered by `test_cache_read_failure_falls_back_to_real_transcription` before this session's larger scope started, landed as its own commit.
- **Dead scaffolding found, not touched:** `backend/workers/stage_workers.py` (`FrameExtractionWorker`, `GroundingDinoWorker`, `Sam2Worker`, `TrackingWorker`, `ProPainterWorker`, `RenderingWorker`, `VisionDetectionWorker`) and their `cli.py` WORKERS entries are leftover scaffolding from Phase 4's original 3-stage milestone example (own docstring: "No real AI, no storage layer"), predating the Phase 5 asset-model restructuring (v10). None are registered in `capability_registry.py`, so the planner can never reach them — confirmed dead, not cleaned up, since removing it wasn't asked for and wasn't in scope here.

## Changelog from v18

**Phase 5H shipped against Amazon S3, not Cloudflare R2** (2026-08-02) — reopened minutes after being cancelled the same day, once EC2 deployment (the actual reason this phase exists at all — see its "why now" framing) went from paused back to resuming. AWS already has an account behind the EC2 plan, so no new card was needed; R2 would still have been free-egress-cheaper, but that's a knowingly-accepted cost given up for not blocking on a Cloudflare signup.

- **`S3Storage` (`backend/storage/s3.py`)** implements every `Storage` method via `boto3`, keeps `endpoint_url` as an optional setting (`None` for real AWS) so R2/B2/MinIO stay reachable later with zero code change — the same reasoning §19 Phase 5H originally gave for choosing `boto3` over an AWS-specific SDK, now actually exercised by leaving the door open rather than closing it.
- **Key generation/validation was pulled up into `backend/storage/base.py`** (`generate_key()`, `is_safe_key()`) so `LocalDiskStorage` and `S3Storage` mint and reject keys by the identical rule, rather than two independently-written traversal guards that could silently drift apart. `LocalDiskStorage` was refactored to call these rather than inlining the logic a second time — behavior-preserving, covered by the existing local-storage test suite passing unchanged.
- **`presigned_url()` added to the `Storage` ABC as non-abstract, default `None`** — `LocalDiskStorage` needed no change at all; `download_artifact` (`backend/api/routes/artifacts.py`) checks it first and 307-redirects (`Cache-Control: no-store`) when it's not `None`, otherwise falls through to the existing streaming/Range-request path unchanged.
- **Tested with `moto[s3]`, not MinIO-in-docker-compose as originally planned** — mocks AWS in-process, so `tests/test_s3_storage.py` (18 tests) needs no container, no real account, no credentials in the repo, and runs exactly as fast as any other unit test. Includes the same hostile-URI parametrized list `test_artifacts_api.py` already used for `LocalDiskStorage`, run again against `S3Storage`, proving both backends reject a garbled/malicious key identically. `test_artifacts_api.py` gained two more tests against a `presigned_url()`-only fake `Storage`: the redirect itself, and confirmation that a stranger gets 404 rather than a redirect (the membership guard runs first because it's a FastAPI `Depends`, resolved before the route body).
- **Migration stance made explicit, not assumed:** switching `STORAGE_BACKEND` to `"s3"` does not migrate any existing `local://` URI — the decision is a fresh EC2 box with a fresh, empty database, since this is a new deploy rather than a resurrection of the terminated one. No copy-and-rewrite script exists; would need writing if a future switch ever has to preserve real rows.
- **`storage_backend` still defaults to `"local"`.** Nothing about this shipment flips any running process onto S3 — that happens only at actual EC2 deploy time, once a real bucket and credentials exist. Full suite: 649 passing (was 629 before this).

## Changelog from v19

**New capability: `remove_segment`** (2026-08-02) — cut a middle piece *out* of a video and rejoin the surrounding pieces, the deliberate complement of `trim` (which keeps a range instead of removing one). Triggered by a live mix-up: "trim from 0:40 to 1:10" was interpreted — correctly, per `trim`'s own contract — as "keep only 0:40-1:10", when what was wanted was "remove that range, keep the rest". `trim` alone cannot express that shape (two survivors on either side of a cut, rejoined into one clip); it needed a real new capability, not a prompt tweak.

- **Not blocked by §20 item 6 ("the one genuinely blocked capability family").** That backlog item is about the *engine* being unable to fan a Result out to multiple downstream stages or fan multiple non-adjacent stages into one; `remove_segment` never leaves single-input/single-output at the `Job`/`Result` level — it's one worker internally cutting and concatenating two ranges of the *same* input in one ffmpeg call, exactly the pattern `merge_worker.py` already established for its own multi-*source* concat. Confirmed with advisor before writing any code, specifically to avoid mistakenly treating this as the blocked item.
- **`backend/workers/remove_segment_worker.py`** drives `run_ffmpeg` directly with a `-filter_complex`, not `media.process_video` (which applies one linear `-vf`/`-af` chain — insufficient here, since two disjoint ranges of one input must each be cut then concatenated). Verified before writing the builder that ffmpeg accepts reusing `[0:v]`/`[0:a]` as the input to two independent filter chains in one `filter_complex` with no `split`/`asplit` needed (one throwaway ffmpeg invocation against a fixture, output duration matched exactly).
- **Simpler than `merge_worker.py`**, not harder: single input means both surviving pieces already share one frame size, one frame rate and one audio-presence, so none of merge's canvas-normalisation, frame-rate conforming, or synthesised-silence logic is needed.
- **Degenerate cases fall back to a single trim, not a 1-input concat**: if the requested range touches (or exceeds) either edge of the actual video, only one side survives, and the filtergraph skips `concat` entirely for that side — concat needs two real inputs, and feeding it a near-empty branch is exactly the kind of fragile edge ffmpeg was verified against before shipping. Real video duration is probed up front specifically to make this determination correctly rather than guessing from the requested `end` value.
- **Preview mode is honoured explicitly, not silently dropped.** `media.py`'s own docstring says `is_preview` is "honoured in exactly one place (`process_video`)" — true of every other capability, but `remove_segment` (like `merge`) can't call `process_video` at all. Rather than leave preview silently ignored (the pre-existing gap in `merge_worker.py`, left as-is since fixing it wasn't in scope here), `PREVIEW_SCALE_FILTER`/`PREVIEW_OUTPUT_ARGS` were promoted from `media.py`-private constants to its public surface (`__all__`) so `remove_segment_worker.py` can apply them itself. Without this, every preview-proposal invocation of `remove_segment` would silently full-res encode — a real slowdown on exactly the flow (fast iteration before committing) preview mode exists for.
- **The fix that actually matters is the registry wording, not the worker.** The user hit this because the *planner* read "trim from 0:40 to 1:10" as keep-range — a new capability changes nothing unless the planner reliably picks it over `trim`. Both descriptions in `capability_registry.py` were rewritten to explicitly cross-reference each other and state their opposite semantics in capital letters (KEEPS vs DELETE), rather than relying on the capability *name* alone to disambiguate.
- **Tests appended to `tests/test_trim_merge_workers.py`** (not a new file) — its docstring already frames trim/merge as "the structural edits," and testing `trim` and `remove_segment` side by side is the clearest documentation of the distinction between them. Includes a synthesized red/green/blue three-second clip (`rgb_thirds_clip`, same technique as `test_scene_detection_worker.py`'s `hard_cut_clip`) so removing the middle (green) second has a mechanically checkable result — not just "output is ~2s long" (which a builder that emitted only one of the two branches could pass by accident), but "the result is red then blue with no green," verified by sampling a single downscaled pixel via ffmpeg at each end. 18 new tests total (9 for the capability itself, 9 parametrized bad-window cases mirroring `trim`'s own).
- **Full suite: 667 passing** (649 + 18). Live stack needs a `remove_segment` worker process started before any manual test — the Kafka topic auto-creates on first publish, but nothing consumes it until a worker of that name is running.

---

## 1. General direction (confirmed, unchanged)

Conversational video editing agent on top of Setu. Setu remains the execution engine. The LLM plans; it never edits. Workers stay deterministic. No change from v1 — restated here because every later decision depends on it.

---

## 2. Architecture shape (confirmed, unchanged)

```
Conversation Layer
      ↓
   Planner
      ↓
Execution (Setu — unmodified)
```

The conversational layer decides *what* to run; Setu decides *how* it runs reliably. This separation is preserved exactly, including through the phase breakdown in §19 — no phase collapses planning and execution together.

---

## 3. Two independent jobs (confirmed, unchanged)

```
Job #1: upload → Video Analysis Worker → metadata stored
                          ↓
        Conversation → Planner → user confirms
                          ↓
Job #2: Edit Workflow (crop, color, audio, subtitle, export, ...)
```

Not merged. Analysis metadata is stored against the **video**, not the job, so it's reusable across any number of later edit jobs against the same upload (e.g. "now make me a TikTok version" doesn't re-run analysis). This was the key structural call in v1 and it stands.

---

## 4. Conversation architecture (simplified per your note)

`messages` table only. A chat turn reads the most recent N messages for the conversation, verbatim, and sends them to the planner as-is. No summarization, no compaction, no semantic retrieval, no vector store. If the history ever gets too long for a prompt, the fix is "send fewer, more recent messages" — not a new subsystem. This is intentionally the simplest thing that works, not a placeholder for something more sophisticated later.

---

## 5. Planner architecture

Still a synchronous service in the API process, never a Kafka worker. Inputs:

1. Conversation — most recent messages, raw (§4)
2. User preferences — the `user_preferences` row for this user (§7)
3. Video metadata — the analysis result attached to this video (§8)
4. Available worker capabilities — the capability registry (§6)

The planner decides which workers to run, in what order, and with what parameters. It never executes anything — no ffmpeg, no OpenCV, no Kafka access, no video bytes.

Because the planner sometimes needs to keep asking questions before it has enough information to propose anything ("Who is this video for?"), its response needs one more bit of structure than the proposal schema alone: a top-level discriminator.

```json
{ "type": "message", "text": "Who is this video intended for?" }
```
or
```json
{ "type": "proposal", "summary": "...", "workflow": [ ... ] }
```

The `"proposal"` case's body is exactly the schema you specified — see §6. This discriminator is the one addition beyond what you wrote; flagging it explicitly rather than silently introducing it, since it's necessary for the "assistant asks clarifying questions before proposing anything" behavior in the spec. (Named `"proposal"` rather than `"plan"` from the start, anticipating Phase 9a's approval workflow — see the principles below and the Phase 9a section; this also matches what actually shipped in Phase 2's `StaticPlanner`.)

---

## AI architecture principles (v6)

Added 2026-07-26, before Phase 3 started, as explicit guidance for Phase 4 onward — nothing below changed any shipped code; it constrains how future phases get built.

1. **Single planner, no multi-agent frameworks.** One LLM call producing one structured response (§6). No LangGraph, CrewAI, reflection loops, planning graphs, or autonomous tool routing, unless a future phase's actual requirement clearly can't be met without one — and if that seems to be happening, **stop and explain the reasoning before implementing**, don't just reach for the framework. This is the literal gate to apply at Phase 4 and Phase 9a specifically — those are the two phases where prompt complexity could tempt one.
2. **Stateless planner.** Every invocation gets all its context explicitly passed in (project metadata, uploaded videos/assets, conversation history, user preferences, approval policy where relevant, the existing proposal when revising one) and returns a structured response and nothing more. No hidden state, no session carried inside the planner itself between calls.
3. **Ordinary services, not agents.** The flow is plain backend services calling each other — prompt/context construction, the LLM call itself, and the Phase 3 validator — not an "agent" abstraction. Phase 4's Components list already describes this flow (prompt construction from conversation + preferences + metadata + registry → call the LLM → parse the response, then the Phase-3 validator gates the result); read informally as roles rather than a required class split, that's context-building = the prompt-construction step, planning = the LLM call/parse step, validation = Phase 3's validator, unchanged either way.
4. **No sophisticated memory.** No vector/semantic/episodic memory, no RAG. §7's `user_preferences` table — one flat row per user, written by one optional lightweight LLM call after a successful edit — is already the full extent of memory for V1, and already satisfies this principle as shipped; nothing there needs to change.
5. **No premature context-length optimization.** Typical project discussions are expected to fit comfortably in modern context windows; summarization machinery is deferred until real usage actually demonstrates a need. Phase 2's `conversation_context_limit` (fixed at 20 recent messages, already shipped) is the simple cap this principle is fine with, not the kind of premature optimization it warns against — don't read this principle as a reason to rip that cap out.
6. **General complexity discipline.** Prefer the simplest architecture that solves the current phase's actual requirement. If, while implementing a future phase, it looks like more orchestration or memory than described above is genuinely needed, stop and explain the reasoning before implementing it — don't add it silently.

---

## 6. Planner output schema (new — formalized per your request)

Canonical shape for the `"proposal"` case:

```json
{
  "summary": "Remove the silent intro, brighten slightly, normalize audio, add captions, export 1080p.",
  "workflow": [
    { "stage": "crop", "params": { "aspect_ratio": "9:16" } },
    { "stage": "color", "params": { "brightness": 1.1 } },
    { "stage": "audio", "params": { "normalize": true } }
  ]
}
```

**Validation, before this touches `JobSubmissionService` at all:**
1. `workflow` is non-empty.
2. Every `stage` name exists in the capability registry (§ below) — an unregistered stage name is rejected outright, never silently passed through.
3. `params` for each stage are checked against that stage's declared parameter shape in the registry (at minimum: known keys, correct types — doesn't need to be a full JSON-Schema validator for V1).
4. Any failure here does **not** reach Setu as a bad job — it's handled in the conversational layer as either a regenerate-and-retry or a clarifying question back to the user ("I'm not sure what aspect ratio you want — could you clarify?").

**Translation into Setu's native shape**, which is what actually gets submitted:

```python
workflow = [item["stage"] for item in proposal["workflow"]]          # -> Job.workflow
stage_params = {str(i): item["params"] for i, item in enumerate(proposal["workflow"])}
payload = {"video_uri": ..., "stage_params": stage_params}           # -> Job.payload
```

(Phase 4 onward: `video_uri` here becomes per-stage `video_uris`, once a proposal can reference more than one video — see Changelog v8 and §19 Phase 4. `Job.payload` stays an opaque dict either way; Setu's core never inspects its shape.)

This is the same "index-keyed stage params live inside the existing freeform `payload`" idea from v1, just now explicitly framed as a compilation step with the planner's schema as its input — the planner never has to know or care that Setu's `Job.workflow` is a flat string list. **This compilation function is the one new piece of code between "planner" and "Setu"** (§19 Phase 3 names it `compile_workflow`, replacing an earlier "translator" working name). Everything on the Setu side of it (`JobSubmissionService.submit(workflow=..., payload=...)`) is called completely unmodified.

**Capability registry** (new, static, lives in code — not a DB table): for every registered worker, its stage name (= topic name = `WORKERS` dict key, same convention Setu already uses), a short description for the LLM prompt, and its accepted parameter keys/types. This is both the LLM's prompt-time contract ("here's what you're allowed to choose from") and the validator's runtime contract ("here's what's actually allowed") — one source of truth for both.

---

## 7. Memory architecture (confirmed, unchanged)

```
user_preferences(
  user_id PK,
  preferred_platform, preferred_export_format, captions_enabled,
  preferred_resolution, subtitle_language, updated_at
)
```

Loaded before every planning call. Written to only after a successful edit, via one optional lightweight LLM call over the conversation ("anything durable worth remembering here?"). No vector DB, no framework, nothing beyond this table. Where this write gets triggered from, given there's no Notification Worker, is addressed in §19 Phase 6.

---

## 8. Video analysis architecture (confirmed, unchanged)

Single-stage Job #1 (`["video_analysis"]`). `VideoAnalysisWorker` is a plain `Worker` subclass — gets idempotency/retry/crash-recovery for free, same as any other worker. Its result (duration, fps, resolution, codec, orientation, brightness/contrast, loudness, silence-at-boundaries, transcript, faces, scene changes, camera motion, blur score) is copied/referenced onto the `videos` row so it survives past Job #1 and is queryable by video, not by job.

---

## 9. Worker architecture (updated — Notification Worker removed)

Updated per v10 — the Phase 5 capability set. One worker per stage name; stage name = topic name = `WORKERS` key throughout.

| Worker | Stage/topic name | Phase | Notes |
|---|---|---|---|
| Video Analysis Worker | `video_analysis` | 1 | Job #1, sole stage |
| Crop Worker | `crop` | 5B | aspect-ratio crop |
| Resize Worker | `resize` | 5B | target width/height |
| Rotate Worker | `rotate` | 5B | 90/180/270 |
| Flip Worker | `flip` | 5B | horizontal/vertical |
| Pad Worker | `pad` | 5B | letterbox/pillarbox to fit |
| Color Worker | `color` | 5C | brightness/contrast/saturation/gamma/sharpen |
| Audio Worker | `audio` | 5D | normalization, silence removal |
| Trim Worker | `trim` | 5E | cut to a time range |
| Merge Worker | `merge` | 5E | concatenate clips (first stage only) |
| Transcribe Worker | `transcribe` | 5F | Groq-hosted `whisper-large-v3` → transcript + srt assets |
| Subtitle Burn Worker | `burn_subtitles` | 5F | burns an srt asset into the video |
| Render Worker | `render` | 5G | final container/resolution/bitrate |
| Stabilization Worker | `stabilize` | — | later, not required for V1 (§20 item 4) |
| Compression Worker | `compress` | — | later, not required for V1 (§20 item 4) |
| Thumbnail Worker | `thumbnail` | — | later, not required for V1 (§20 item 4) |

Spatial transforms are separate stages rather than one polymorphic `transform` because they are order-sensitive (crop-then-scale ≠ scale-then-crop) and the workflow list is where that order is already expressed. `color`'s adjustments are grouped into one stage for the opposite reason — one filtergraph, no order-sensitivity between them. See §19 Phase 5.

~~Notification Worker~~ — **removed for V1**. The frontend submits a job, gets a `job_id`, and polls `GET /jobs/{id}` (already implemented, already returns status/progress). This is strictly less to build, and it's honest about what V1 actually needs: nothing currently consumes a "job done" event except a UI that could just as easily ask. Revisit only if polling proves too slow/chatty in practice — see §20.

Planner is deliberately absent from this table — it's not a Kafka worker, it lives in the API process (§5).

---

## 10. Event flow (updated — no lifecycle event/notification hop in V1)

**Job #1 — Analysis:**
```
video.uploaded → [outbox] → video_analysis topic → VideoAnalysisWorker → Result
  → metadata copied onto video row
```

**Conversational planning (no Kafka):**
```
video has metadata → chat turns → planner → proposal produced → user confirms
  → proposal validated + compiled (§6) → Job #2 submitted
```

**Job #2 — Edit:**
```
[outbox] → crop → CropWorker → Result
       → color → ColorWorker → Result
       → audio → AudioWorker → Result
       → subtitle → SubtitleWorker → Result
       → export → ExportWorker → Result
```

No further hop after the last stage in V1 — the frontend already knows a poll will show `status: completed`. Whatever triggers the memory-update LLM call (§7) is a concern of the polling endpoint, not of a Kafka consumer — see §19 Phase 6 for exactly where that hook lives.

---

## 11. Job lifecycle (confirmed, unchanged)

Same state machine, same engine, used identically for both jobs. Not modified in any phase below.

---

## 12. Components that remain unchanged (confirmed, unchanged)

`JobSubmissionService`, `WorkflowEngine`, `StageProcessingService`, `WorkerRunner`, `Worker` ABC, `OutboxRepository`/`OutboxPublisher`, all existing models/repositories, observability stack, `jobs` API routes, docker-compose infra. Every phase in §19 through Phase 7 is designed so none of these need to change. From Phase 8 onward that hard rule relaxes to a strong default — prefer building above Setu, but a modification with a documented good reason is allowed (see the posture note at the top of Phase 8; Phase 9b's cooperative cancellation is the first such case).

---

## 13. Components to add (updated)

- Storage abstraction (§14, first priority — see Phase 0)
- `videos`, `conversations`, `messages`, `user_preferences` tables
- Video Analysis Worker + new editing workers (§9)
- Capability registry (code, not DB) + proposal validator/compiler (§6)
- Planner service (LLM call + the two-shape response)
- Chat endpoint(s)
- ~~Notification delivery mechanism~~ — not needed; `GET /jobs/{id}` polling covers it

---

## 14. Database additions (updated per v5 — see changelog)

```
projects(id, owner_id, name, created_at, updated_at)

videos(id, project_id FK -> projects.id (NOT NULL, CASCADE), storage_uri,
       original_filename, name (nullable, user-entered display name),
       latest_analysis_job_id FK -> jobs.id (nullable),
       created_at, updated_at)

conversations(id, project_id FK -> projects.id (NOT NULL, UNIQUE),
              created_at, updated_at)

messages(id, conversation_id FK, sender_id (nullable), role, content,
         created_at)

user_preferences(user_id PK, preferred_platform, preferred_export_format,
                  captions_enabled, preferred_resolution, subtitle_language,
                  updated_at)
```

`Project` is the aggregate root everything above hangs off (v5) — no video, conversation, or message exists independent of one. No `proposals` table before Phase 9a (a confirmed proposal before then is just what's already on the submitted `Job` row); Phase 9a adds one for the approval workflow (§19). No vector store, no generic events table.

---

## 15. New Redpanda topics (updated)

One topic per stage name, matching §9's table (updated per v10): `video_analysis`, `crop`, `resize`, `rotate`, `flip`, `pad`, `color`, `audio`, `trim`, `merge`, `transcribe`, `burn_subtitles`, `render` (+ `.dlq` for each, already generic, no config needed). Topics are created per capability as its sub-phase lands, not all up front. ~~`job-lifecycle`~~ — dropped for V1 along with the Notification Worker; see §20 if it's ever needed.

---

## 16. Responsibilities recap (confirmed, unchanged)

- **Planner**: decide stages/order/params from the registry; ask clarifying questions; never execute.
- **Orchestrator** (`WorkflowEngine` + `StageProcessingService`): own workflow position and transactional dispatch; unaware this is video.
- **Workers**: one deterministic operation each; never call the LLM; never talk to each other; never decide workflow.

---

## 17. Conversation & memory flow (confirmed, with the trigger question resolved)

Conversation flow unchanged from v1: chat → planner → message-or-proposal → confirm → validate/compile (§6) → submit Job #2 → poll for completion.

Memory flow's open question from v1 — "what triggers the post-success preference-update call, without a Notification Worker?" — is resolved explicitly, not via a side effect on a GET: the frontend polls `GET /jobs/{id}` (read-only, no side effects) and, on observing `status: completed`, calls `POST /jobs/{id}/update-memory` (§19 Phase 6). That endpoint loads the completed conversation, runs the one lightweight preference-update LLM call, upserts `user_preferences` if warranted, and returns. It's idempotent, so the frontend calling it more than once (double-poll, retry, multiple tabs) is harmless. If Setu later gains a real terminal lifecycle event (§20), this same logic moves into a background consumer of that event without changing anything about the endpoint's contract or the overall architecture — only what triggers it changes.

---

## 18. Failure scenarios (confirmed, unchanged from v1)

Corrupt video / unsupported codec, planner failure, worker crash, export failure, in-flight proposal change — all handled exactly as described in v1 (retry/backoff/DLQ for anything inside Setu's Kafka path; local retry+graceful chat message for planner failures, since nothing durable has been submitted yet). Not repeated in full here; nothing about the refinements in this update changes any of those mechanics.

---

## 19. Implementation roadmap

Each phase is independently demoable and none requires reopening a prior phase's code — later phases only add new workers/tables/endpoints, never restructure what's already there.

### Phase 0 — Storage foundation

**Goal.** A place for uploaded video bytes and per-stage artifacts to live, with a worker-readable URI scheme. Nothing else in this plan can run without it.

**Why before Phase 1.** Video Analysis Worker needs a real file to read; the API needs somewhere to put an upload before it can even create a Job.

**Components.** `backend/storage/` gets a small interface (`put`, `get_uri`, `exists`) with a local-disk implementation. No S3 yet — the interface is what makes swapping that in later a config change, not a rewrite.

**Database changes.** None yet (the `videos` table lands in Phase 1, once there's something to attach storage to).

**API changes.** None yet — storage is exercised directly in tests first; the upload endpoint arrives in Phase 1 alongside it.

**Worker changes.** None.

**Frontend changes.** None.

**Testing strategy.** Unit tests against the storage interface: put a file, get it back by URI, confirm non-existent URIs error cleanly.

**Demo scenario.** A script (or test) writes a video file through the storage interface and reads it back byte-for-byte.

**Risks.** Picking a URI scheme that's awkward to later map onto S3 keys — keep it a plain opaque string from day one so nothing downstream parses its structure.

---

### Phase 1 — Upload + Video Analysis Worker (Job #1, end to end)

**Goal.** A real video, uploaded through the API, gets analyzed by a real (if initially simple) worker, and the metadata lands somewhere queryable — proving the *entire* Job #1 path end to end, through Setu's existing, unmodified machinery.

**Why before Phase 2.** The conversation layer's whole reason to exist is to plan against real metadata; there's nothing to plan against until this phase exists.

**Components.** `VideoAnalysisWorker` (new `Worker` subclass, registered in `WORKERS`); upload endpoint that writes to storage (Phase 0) and calls `JobSubmissionService.submit(workflow=["video_analysis"], payload={"video_uri": ...})` **unmodified**.

**Database changes.** `videos` table (§14), belonging to a `Project` (v5 — every video requires one; `POST /projects` creates it). `latest_analysis_job_id` populated once the worker succeeds.

**API changes.** `POST /projects/{project_id}/videos` (upload) → creates `videos` row + Job #1, returns `video_id` + `job_id` (v5 — was flat `POST /videos`). `GET /videos/{id}` stays flat. `GET /projects/{id}/videos` lists a project's videos — multiple videos per project already works, no unique constraint blocks it. `GET /jobs/{id}` already exists and needs no change to report Job #1's progress.

**Worker changes.** One new worker. Start with a subset of the metadata list (say: duration, fps, resolution, codec) if the full analysis stack (Whisper, scene detection, etc.) isn't ready yet — the point of this phase is proving the *pipeline*, not shipping every metric on day one. Add the rest incrementally without touching anything else.

**Frontend changes.** None yet (or a bare upload form if you want something clickable early).

**Testing strategy.** Integration test: upload a small test video, poll until `completed`, assert the `videos` row has metadata. Also re-run the existing kill-worker-mid-job test pattern against this new worker to confirm crash recovery holds for it too — it should, for free, but worth proving once.

**Demo scenario.** Upload a video via the API, watch Job #1 go `pending → running → completed`, see real metadata on the video record.

**Risks.** Being tempted to build the full analysis stack (Whisper + scene detection + face detection all at once) before proving the plumbing — resist; ship partial metadata first, expand later, each addition needs no other change. `videos.name` — an optional user-entered display name, distinct from `original_filename` — was added the same way this note originally predicted it could be: a nullable column and an optional form field, no restructuring required.

---

### Phase 2 — Conversation mechanics (stub planner)

**Goal.** Prove the chat loop — messages persisted, ordered, retrievable — without yet depending on a real LLM.

**Why before Phase 3/4.** Decouples "does the conversation plumbing work" from "is the LLM prompt good," so a bad prompt later doesn't also mean debugging persistence at the same time.

**Components.** `conversations`/`messages` tables and repositories; a chat endpoint; a **stub planner** that returns a fixed, hardcoded `{"type": "proposal", ...}` response (or a fixed clarifying question on the first turn) regardless of input. `user_preferences` table also created here (empty rows, sensible defaults) so the read-path wiring exists early even though nothing writes to it until Phase 6.

**Database changes.** `conversations`, `messages`, `user_preferences` (§14).

**API changes.** `POST /projects/{id}/messages` (post a chat message, get the assistant's stubbed reply back), `GET /projects/{id}/messages` (history) — project-scoped from the start (v5: `Conversation` belongs to `Project`, not `Video`), matching how this actually shipped.

**Worker changes.** None.

**Frontend changes.** None yet.

**Testing strategy.** Post several messages, confirm ordering and persistence; confirm the stub planner's fixed response is returned and appended as an assistant message.

**Demo scenario.** A short scripted back-and-forth against the API showing message history growing correctly, with a canned "proposal" coming back on cue.

**Risks.** Under-scoping "recent N messages" — pick a concrete number now (e.g. last 20) rather than leaving it open, since that number is a real prompt-budget decision (§4).

---

### Phase 3 — Capability registry + proposal validation/compilation (still no real LLM)

**Purpose (added 2026-07-27, before implementation).** This phase establishes the proposal validation and workflow compilation pipeline. It intentionally introduces no AI-specific architectural decisions — no orchestration, no memory systems, no multi-agent workflows, no advanced planning (see the AI architecture principles under §5). Its only responsibility is proving that a valid proposal can be converted into an executable Setu workflow.

**Goal.** Build the registry and the validator/compiler from §6, and prove a proposal (still hand-authored, not LLM-generated) can be turned into a real Setu Job #2 and executed — using the **existing `dummy` worker already in the codebase** as a stand-in stage, not new throwaway fake workers.

**Why before Phase 4.** Isolates "is the planner→Setu translation correct" from "is the LLM's output good" — the same reasoning as Phase 2. If Phase 4 later produces a bad job, you'll already know the compilation layer isn't the cause.

**Components (revised 2026-07-27 for extensibility, before any code was written — see conversation for full rationale on each):**
- `CapabilityRegistry` — a small class (`get`/`exists`/`list`/`register`) wrapping an internal dict, not a bare module-level constant. The validator depends on this interface, not a global, so Phase 5 can register real workers one at a time without touching validator logic. Still backed by a hardcoded dict for V1.
- `StageCapability` — `name`, `description`, `parameter_schema` (renamed from `params` — same meaning, clearer). Deliberately **not** adding `worker_name`, `category`, `version`, or `supports_batch` yet: `worker_name` would duplicate the identity §6 already establishes (`stage name = topic name = WORKERS dict key`), and the other three have no consumer anywhere in Phase 4 or Phase 5's plan as written. Added in whichever sub-phase first needs them — cheap, since this is a dataclass in code, not a migration.
- `Proposal` / `ProposalStage` — small frozen dataclasses (matching `StageMessage`'s existing convention in `workers/base.py`) replacing raw dicts inside the validation/compilation pipeline. Pydantic stays the API-schema layer only (see `api/schemas/`); these are internal domain objects, not request/response bodies, so a plain dataclass is the convention match, not Pydantic.
- Validator returns a `ValidationResult` (`valid: bool`, `errors: list[str]`) instead of only raising — and **collects every error found**, not just the first (unregistered stage *and* malformed params in the same proposal both get reported together). This matters concretely at Phase 4: the planner's regenerate-and-retry loop feeds validation errors back into the LLM prompt, and "unknown stage 'crp', unknown param 'britness' on 'color'" is a far better retry signal than one error at a time.
- `ExecutionContext` — a one-field dataclass (`video_uri: str`) passed alongside the `Proposal` to the compiler, instead of a bare `video_uri=...` keyword. Keeps the compiler's signature stable for Phase 4+, when more than a single URI will need to travel alongside a proposal.
- The compiler (`compile_workflow(proposal, context) -> (workflow, payload)`, replacing "translator") stays a **pure function** — no DB, no storage, no API calls, no job submission, no state mutation. Renamed from "translator" because its future responsibilities (asset resolution, param normalization, default injection, shorthand expansion) are closer to compilation than plain translation.
- Registry/validator/compiler are already asset-agnostic — they only ever see `stage` and `params`, never a video — so no video-specific naming needs correcting there. `ExecutionContext.video_uri` stays as-is: `Video` is the only asset-like model that actually exists in this codebase today, so naming the field `asset_uri` would be less accurate, not more, until an `Asset` concept is real.

**Scope boundary — what does NOT change in Phase 3.** `Planner`/`StaticPlanner` (`backend/services/planner.py`) and `ConversationService` still pass and store a plain `dict[str, Any]` (via `json.dumps` into `Message.content`) — Phase 3 has no wiring into the conversation flow at all, so there is nothing there to convert yet. `Proposal`/`ProposalStage` are introduced only inside the new validation/compilation pipeline. Converting the planner's output into these domain models is Phase 4's job, when the real LLM response is parsed for the first time.

**Duplicate stage names.** Rejected for V1. Setu's engine would mechanically tolerate a repeated stage (`stage_params` is index-keyed, `Result` rows are keyed by `(job_id, stage_index)`, dispatch is positional) — so this is a deliberate validator policy, not a hard constraint from Setu, and it's asserted with a test rather than left undefined. Rationale: none of V1's five editing stages (crop/color/audio/subtitle/export) has a legitimate reason to run twice in one job, and a duplicate is far more likely to be an LLM hallucination than an intentional request — easy to loosen later if a real use case shows up.

**Database changes.** None new.

**API changes.** None new. The optional debug endpoint mentioned in earlier drafts is skipped — the integration test already exercises the full pipeline (proposal → validation → compilation → `JobSubmissionService` → `WorkflowEngine` → `DummyWorker` → completed `Job`), and the real user-facing entry point arrives in Phase 4 when the planner is wired into the conversation flow.

**Worker changes.** None new — reuses the existing `dummy` worker, registered under a stand-in stage name in the registry purely to prove the wiring.

**Frontend changes.** None.

**Testing strategy.** Unit tests: a proposal with an unregistered stage is rejected; a proposal with malformed params is rejected; a proposal with duplicate stage names is rejected; a proposal with multiple simultaneous errors reports all of them via `ValidationResult`; a valid proposal compiles to the exact expected `workflow`/`payload` shape. One integration test: submit a hand-written valid proposal through the full path and see the resulting Job complete via the existing engine.

**Demo scenario.** Feed a hand-authored `{"summary": ..., "workflow": [...]}` JSON into the validator/compiler, show it becomes a real `Job` that runs to completion — with zero changes to `JobSubmissionService`/`WorkflowEngine`/`StageProcessingService`.

**Risks.** Over-building the param validator (full JSON-Schema, custom DSL, etc.) — a flat "known keys + basic type check" is enough for V1; tighten only if bad proposals actually get through in practice.

---

### Phase 4 — Real planner LLM (revised per Changelog v8)

**Goal.** Replace `StaticPlanner` with `LLMPlanner`, reusing the entire Phase 3 proposal pipeline unchanged in spirit (validator logic untouched; compiler's responsibility unchanged, only its per-stage video shape grows). One question this phase answers and nothing more: *can a real LLM generate a valid proposal that executes successfully through the existing Setu pipeline?* Built per the v6 AI architecture principles: one stateless planner call per turn, no agent framework, no memory beyond §7, no premature architecture for phases that aren't here yet.

**Why before Phase 5.** The planner needs *some* registered stages to plan against; Phase 3 already proved the plumbing against `dummy`, so swapping in the real LLM here is a contained change — only the "what produces the proposal JSON" piece moves, nothing around it does.

**Components.**
- **`PlannerContext`** — replaces "many unrelated arguments" into the planner with one dataclass: project, conversation history, the project's videos (each exposed with a short LLM-facing handle, e.g. `video_1`, not a raw UUID), user preferences, the capability registry, and an `approval_policy` field that's a future-compatible placeholder (unused until Phase 9a). Assembled by `ConversationService` before every planning call; the planner never fetches data itself.
- **`PromptBuilder`** — `PlannerContext → Prompt`, extracted as its own service so future prompt types (clarification, revision, summaries) reuse the same construction step.
- **`LLMClient` / `GroqClient`** — a thin abstraction so `LLMPlanner` depends on an interface, not the `groq` SDK directly. Only one concrete implementation ships now; other providers are not stubbed out ahead of need. Uses Groq's schema-enforced `response_format={"type": "json_schema", ...}` — not prompt-engineered JSON.
- **`LLMPlanner(Planner)`** — `respond(context: PlannerContext) -> PlannerResponse`. **This changes `Planner`'s interface signature** (was `respond(messages, preferences) -> dict`); `StaticPlanner` and its Phase 2 tests move to the new signature too, since both implement the same ABC. Both the input and the output are now typed domain objects — `PlannerResponse` (`type` / `message` / `proposal`, where `proposal` is Phase 3's existing `Proposal`) replaces the raw `dict[str, Any]` on both sides of the call, per the "avoid raw dictionaries" principle. `ConversationService` is the only caller and now serializes `PlannerResponse` into `Message.content` itself.
- **Two-layered, bounded retry.** Infrastructure retry (network failure, provider timeout, malformed/non-parseable structured output) is separate from semantic retry (Phase 3's `validate_proposal` rejects the proposal → regenerate exactly once with the `ValidationResult` errors fed back into the prompt → if still invalid, return a friendly clarification message instead of a job). No recursive retries, no reflection/self-critique loop, no unbounded regeneration.
- **`validate_proposal` is reused exactly as implemented in Phase 3** — no changes to that file in this phase (see the known Phase 5 landmine below).
- **`compile_workflow`'s video shape changes, its responsibility doesn't.** `ExecutionContext.video_uri: str` → `ExecutionContext.video_uris: dict[str, str]` (handle → resolved storage URI, built by `ConversationService`); `ProposalStage` gains `video_ids: list[str]`. Still a pure `Proposal → Workflow` function — this is the Multiple Videos decision below, not a new "asset resolution" responsibility.
- **Multiple videos are in scope for Phase 4.** A project already holds many `Video` rows; the planner now explicitly chooses among them per stage via handles, rather than every stage implicitly sharing one project-wide video. This is deliberately narrower than the `Asset`/`AssetRegistry` system deferred to Phase 5+ (see below) — just an id→uri dict over already-known `Video` rows.
- **Lightweight observability**: provider, model, latency, prompt/completion tokens, whether regeneration was used, validation success/failure. Additive logging only, no telemetry redesign.

**Database changes.** None new — confirm-proposal obtains the latest proposal by scanning conversation history, not from a new table (proposal persistence stays deferred to Phase 8/9).

**API changes.** Chat endpoint now round-trips through `LLMPlanner`. **`POST /projects/{id}/confirm-proposal`** is the sole confirmation entry point (no free-text chat confirmation): it scans the conversation backwards for the most recent `{"type": "proposal"}` message (not just the last message, since a clarifying turn can follow a proposal), compiles it, resolves video handles to URIs, and calls `JobSubmissionService.submit()`. Idempotent via Setu's existing idempotency-key mechanism (a deterministic key derived from the conversation id + proposal message id) — calling it twice replays the same `Job`, never creates a second one.

**Worker changes.** None new yet — still targets whatever's registered (§9's real workers land in Phase 5; until then, keep `dummy` registered so this phase is fully testable on its own).

**Frontend changes.** None yet, unless useful to demo manually via a REST client.

**Testing strategy.** Prompt-level tests with representative conversations (vague request → clarifying question; specific request → valid proposal referencing one or more videos; nonsense request → graceful handling); infra-retry test for a simulated LLM failure/timeout; semantic-retry test proving exactly one regeneration attempt on validation failure, then a clarification message; `confirm-proposal` idempotency test (two calls, one `Job`).

**Demo scenario.** The example from the spec, live: "Make this look more professional" → assistant asks who it's for → "LinkedIn" → assistant proposes a concrete workflow → user calls `confirm-proposal` → a real Job #2 is submitted (still against `dummy` until Phase 5).

**Risks.** LLM hallucinating a stage name, malformed params, or a video handle that doesn't exist — all caught by Phase 3's unmodified validator plus `ConversationService`'s handle resolution; if it's tripping constantly, that's a prompt/registry-description problem, not a reason to loosen validation. Separately: `validate_proposal`'s duplicate-stage-name rejection can't trigger in Phase 4 (registry only has `dummy`) but becomes live in Phase 5. **The fix this document originally proposed — re-keying on `(stage name, video_ids)` — is now known to be wrong**; see Phase 5A, which resolves it differently and explains why.

**Deferred out of this phase (Changelog v8):** richer `StageCapability` fields, any `compile_workflow` responsibility beyond `Proposal → Workflow`, the generic `Asset`/`AssetRegistry`/`AssetResolution` system, and reshaped worker/capability metadata all wait for Phase 5; proposal persistence/versioning/revisions/approval-engine/collaborative-editing/ownership wait for Phase 8/9.

---

### Phase 5 — Media capabilities on an asset model (revised per Changelog v10)

**Goal of the phase as a whole.** Replace `dummy` with real media processing, one capability at a time. Each sub-phase is a complete, shippable milestone on its own — the system is fully working and demoable after every single one. This containment is the whole point: if any one capability runs far longer than expected (transcription is the known candidate), that risk stays isolated and never blocks the others from already being done.

This phase is where the product starts existing. Everything through Phase 4 is a complete conversational pipeline that produces nothing — the planner proposes correctly, the job executes flawlessly through retry/DLQ/crash-recovery, and `dummy` returns `{"processed_by": "dummy"}`. Phase 5 is what makes that machinery mean something.

**Why before Phase 6.** Memory only has something worth remembering once real edits are actually happening — no point wiring "did the user like this edit" against fake output.

**Philosophy — deterministic execution only.** No AI reasoning happens in this phase. The planner has already decided what to do; Phase 5 executes media operations. Every capability is deterministic, independently testable, composable, free of hidden side effects, and never touches conversations or planner state.

**Capabilities, not workers (already true, restated).** The planner only ever sees `CapabilityRegistry`/`StageCapability` — name, description, param schema. It has zero knowledge of `Worker`, the `WORKERS` registry, or any execution detail, and never imports `backend.workers`. `Worker` already *is* the internal implementation a capability is expressed through. No restructuring is needed to "become capability-oriented"; the existing seam already does it.

**Assets, not just videos (new — the one real model change).** A stage no longer implicitly produces "a video." It produces a list of typed assets: `{"kind": "video" | "transcript" | "srt" | "thumbnail" | "audio" | ..., "uri": ...}`. This is necessary, not decorative — transcription produces a transcript and a subtitle file, not a video, and no single `output_uri` key extends to that. Scope stays deliberately small: a typed value object plus shared helpers, no `AssetRegistry` and no resolution engine (Changelog v8 already anticipated this landing at Phase 5).

**Chaining stays worker-side, via `previous_output`.** `WorkflowEngine` documents a real invariant — it never imports from `backend/workers/` and treats `message.payload` as opaque. `previous_output` (the prior stage's `Result.payload`, injected by `StageProcessingService`) is Setu's existing, purpose-built mechanism for exactly this, already used by the `frame_extraction → … → rendering` chain. Teaching the orchestrator to understand a typed asset list would be a strictly larger coupling for no gain. Chaining is implemented by **monotonic asset accumulation**: every stage forwards every asset it received, replacing the video asset only if it actually edited the video. Once `transcribe` emits an `srt`, every later stage carries it forward — so producer and consumer never need to be adjacent.

**Database changes:** none new in this phase. **API changes:** artifact listing/download and preview submission (5A, below) — the first endpoints that let a client actually retrieve output. **Frontend changes:** none yet; Phase 7 is where these become visible.

---

**Phase 5A — Shared media infrastructure**

Everything later capabilities reuse. No user-visible capability ships here; this is the foundation that makes the rest small.

- **`backend/workers/media.py`** (new): `Asset` frozen dataclass (`kind`, `uri` — matching the existing `Proposal`/`ProposalStage`/`StageMessage` convention, serialized at the `Result.payload` boundary); `primary_video(assets)` — the single function every capability uses to select the chainable video, so no two workers can disagree; `forward_assets(previous, produced)` — monotonic accumulation, implemented once rather than restated per worker; `materialize_to_tempfile(uri, suffix)` and `run_ffmpeg(...)` following `video_analysis_worker.py`'s proven pattern (Windows-safe close-before-subprocess, `finally` cleanup).
- **`run_ffmpeg` takes a filter list, not raw args**, so it owns filtergraph composition — required for preview mode (below) to append its own scale filter without clobbering the caller's.
- **Error hierarchy**: `MediaProcessingError` (retryable — ffmpeg missing or crashed, an environment problem) and `InvalidMediaParamsError(MediaProcessingError, PermanentError)` (bad params or unusable input — fails identically every retry, so straight to DLQ). Mirrors `VideoAnalysisError`/`UnsupportedVideoError`.
- **`StageCapability` gains `requires_asset_kinds` / `produces_asset_kinds`** (defaulting to `["video"]`/`["video"]`, so most capabilities need no extra config), and **`validate_proposal` gains a forward-scan check**: it accumulates available asset kinds stage by stage and rejects a proposal whose stage needs an asset nothing before it produces. This routes the failure into Phase 4's existing semantic-retry loop so the LLM self-corrects — rather than letting it become a runtime DLQ, which is the exact failure class fixed in Phase 4 for video handles.
- **`validate_proposal`'s duplicate-stage-name rejection is dropped, not re-keyed.** Phase 3 introduced it as an explicit V1 policy (Setu's engine tolerates repeats mechanically); Changelog v8 then proposed re-keying it on `(stage, video_ids)` when Phase 5 landed. **Both are now wrong.** Under asset chaining a repeated stage is *meaningful*, not a mistake — each instance operates on the previous one's output. `trim → trim` ("drop the intro, then drop the boring middle") is an ordinary workflow, and `crop → crop` is legitimate too. The proposed re-key also wouldn't have worked: only stage 0's `video_ids` are real, since every later stage takes its input from `previous_output`, so two downstream `trim`s would key identically anyway. Removing the check is the correct resolution; asset-availability validation (below) is what actually guards proposal correctness now.
- **`StageProcessingService._record_success` populates `Result.artifact_uri`** from the primary video asset. That column has existed since it was added and has never been written — this finishes a mechanism the schema already anticipated, and makes each stage's artifact directly queryable instead of buried in JSON. A deliberate, documented Setu-core touch (posture note, §12/Phase 8): purely additive, existing workers keep getting `NULL` exactly as today.
- **Artifact retrieval endpoints** — `GET /jobs/{id}/artifacts` (list per stage, by asset kind) and a byte-serving download route validated through `LocalDiskStorage`'s existing path guard. Called out explicitly because **nothing in the API can currently serve file bytes at all**: storage URIs are opaque `local://…` strings, so without this there is no way for any client to retrieve output, and no way to verify 5B–5G except by inspecting the filesystem by hand.
- **Preview mode** — a preview is not a new stage; it is the *same workflow compiled in a different mode*, submitted as an ordinary Job (inheriting retry/DLQ/idempotency/observability for free). A payload flag is honored in exactly one place, `run_ffmpeg`: cap resolution and swap to a fast encoder preset. `POST /projects/{id}/preview-proposal` mirrors `confirm-proposal` with its own idempotency-key namespace so previews never collide with real submissions. Frame-only preview (`-frames:v 1` at sampled timestamps, no encode) is the near-instant variant for checking framing and color.
- **Time-range params** are a cross-cutting convention, not a capability: any capability may accept optional `start`/`end` to scope its effect to a segment.
- **Testing**: `LocalDiskStorage(tmp_path)` + monkeypatched `get_storage`, an `ffmpeg_available` fixture alongside the existing `ffprobe_available` one, `tests/fixtures/sample.mp4` as known input. Verification uses ffmpeg's own `signalstats`/`loudnorm` analysis filters rather than adding Pillow/OpenCV/numpy. 5A's own logic (`forward_assets`, `primary_video`, the validator scan) is pure and testable with no ffmpeg at all.
- **Fix `test_retry_backoff_increases_between_attempts`'s flakiness** (`tests/test_worker_retry_and_dlq.py`) — pre-existing, surfaced during 5A. It asserts `deltas[0] < deltas[1] < deltas[2]` on wall-clock time between `consume_one()` calls, but the first call also absorbs Kafka consumer-group join and metadata-fetch cost, so under load `deltas[0]` can exceed `deltas[1]` and the test fails intermittently. Nothing is wrong with the backoff itself. Fix by extracting `runner.py`'s inline `min(base * 2 ** (attempt - 1), max)` into a small pure function and asserting the progression on *that* — deterministic, no sleeping — while the integration test keeps only a loose "retries do get slower" check. Scheduled into this sub-phase rather than the backlog because Phase 5's whole build method is "full suite green after every step," and a test that fails at random directly undermines the ability to tell whether a step broke something — which it already did once, costing a diagnosis round to prove a Step 6 failure wasn't Step 6's fault.

**Phase 5B — Spatial transforms** — `crop`, `resize`, `rotate`, `flip`, `pad`

- **Separate capabilities, not one polymorphic `transform`.** These are order-sensitive (crop-then-scale ≠ scale-then-crop), and a flat optional-params bag cannot express order. Separate stages let the proposal's own `workflow` ordering *be* the filter order — explicit, and already solved by existing machinery.
- `crop` ships first (`aspect_ratio`, matching what Phase 4 already demos conversationally); the rest follow within the same sub-phase on the same skeleton with disjoint simple params (`width`/`height`, `degrees`, `direction`, `pad_color`).
- **Acceptance.** Requested geometry verified via ffprobe; a malformed param reaches `dead_lettered` through the existing retry/DLQ path, unchanged.

**Phase 5C — Visual adjustments** — `color`

- **One grouped capability**, unlike 5B: `brightness`, `contrast`, `saturation`, `gamma`, `sharpen` all map onto a single ffmpeg filtergraph (`eq` + `unsharp`) with no order-sensitivity between them, and users naturally ask for them together ("brighter and more saturated").
- **Acceptance.** Measured `signalstats` shift in the requested direction (not exact-match — encoding isn't lossless).

**Phase 5D — Audio processing** — `audio`

- `normalize` (ffmpeg `loudnorm`, EBU R128), `remove_silence` (`silenceremove`), `preserve_music` (skips silence removal — a V1 simplification, no music/speech classification; recorded as a known simplification rather than pretending it's solved).
- **Acceptance.** Output loudness within tolerance of target; silence-trimmed duration reflects the removed segment.

**Phase 5E — Temporal & structural** — `trim`, `merge` (**`remove_segment` added later, changelog v19** — see below; this phase's own "cut out the boring middle" framing of `trim` is exactly the phrase that later caused a real mix-up, since `trim` keeps a range rather than removing one)

- **`trim`** (`start`, `end`) is the highest-value-per-effort capability in the entire phase and was missing from every earlier draft of this plan. Without it the product can enhance a video but cannot *edit* one — "cut out the boring middle" is the single most common editing operation there is, and its absence is what separates a video enhancer from a video editor. One input, one output, ffmpeg `-ss`/`-to`; no engine changes.
- **`merge`** (concatenate several clips) works **as a first stage only**, and needs no engine change to do so: `compile_workflow` already builds `video_uris` as a *list* per stage, and stage 0's handles are real, so a first-stage merge reads multiple real inputs today. Mid-chain merge and `split` (one output becoming many) stay out of scope — both are genuinely blocked, by `Result`'s `UniqueConstraint(job_id, stage)` (nowhere for one-to-many output to land) and by `previous_output` sourcing only stage N-1 (nowhere for multi-source input to come from). Lifting those is a real execution-model redesign, deferred to its own architecture pass (§20) rather than hacked around here.
- **Acceptance.** Trimmed output duration matches the requested range; a first-stage merge of N clips produces one video of ~the summed duration.

**Phase 5F — Transcript & subtitles** — `transcribe`, `burn_subtitles`

- **Split into two capabilities.** `transcribe` produces `transcript` + `srt` assets and passes the video through untouched; `burn_subtitles` consumes video + srt and produces a new video. The split is what makes the `.srt` a first-class downloadable artifact (people upload subtitle files to YouTube directly) rather than only existing as burned-in pixels. Monotonic accumulation (5A) means the two never need to be adjacent.
- **Transcription runs on Groq's hosted `whisper-large-v3`** (revised per Changelog v11; an earlier draft specified running `faster-whisper` locally). Same model weights, so the "no compromise on transcription quality" decision is unchanged — only where the inference happens moves. `groq>=1.6.0` is already a dependency for the planner and its SDK already exposes `audio.transcriptions.create` with `timestamp_granularities`, which is exactly what SRT cue timings need, so **this sub-phase adds no new dependency at all.**
- **What that removes:** a ~3GB model download, 1.5–3GB of resident RAM during inference, and CPU-bound transcription running at roughly 1–2× realtime on a small instance. Whisper was the single reason the deployment target needed 8GB; without it a 4GB box is comfortable. It also stops being "the sub-phase most likely to run long".
- **What it costs, stated plainly:** transcription now needs the network at job time. That is not a new failure domain — planning already depends on Groq — and it maps onto the existing error split without inventing anything: a timeout or connection failure is a retryable `MediaProcessingError`, a rejected file is a permanent `InvalidMediaParamsError`. Groq caps upload size (~25MB on the free tier), which is why the worker sends *extracted audio* rather than the video: `-ar 16000 -ac 1 -b:a 32k` is around 14MB per hour of speech, so even long clips fit with room to spare.
- **Sequenced after 5D** so duration-changing audio edits (`remove_silence`) run *before* transcription, keeping subtitle timing aligned with the final video. Known, accepted, undetected limitation: asset *presence* is validated, asset *timing validity* is not — a future duration-changing capability inserted between `transcribe` and `burn_subtitles` would drift, and nothing catches it. Out of scope for this phase; recorded so it isn't rediscovered as a surprise.
- **Acceptance.** Transcript spot-checked against known speech (substring, not exact — ASR varies); burn-in presence confirmed by frame comparison rather than OCR.

**Phase 5G — Render & export** — `render`

- **A normal capability, not a special final stage** — `WorkflowEngine` already treats "last stage" generically, so no special-casing is warranted or needed.
- `resolution`, `format`, `bitrate`; writes the final artifact through the storage abstraction. Its `Result.artifact_uri` is precisely what Phase 7's download link consumes — direct payoff from 5A's infrastructure change.
- **Acceptance.** Final artifact is valid and playable in the requested format (ffprobe sanity check); a full multi-capability chain completes with every intermediate `Result` present and correctly chained in Postgres, proving the whole chain ran rather than just the last stage.

---

### Phase 5H — Object storage (Amazon S3) — **SHIPPED 2026-08-02**

**Cancelled as R2 (2026-08-02, no Cloudflare account without a card), un-cancelled minutes later the same day once EC2 deployment — the actual trigger for needing this — resumed, and rebuilt against Amazon S3 instead** (the user already has an AWS account for EC2, so no new card is needed there). Egress cost is a real, knowingly-accepted trade against the original R2 rationale below — recorded rather than re-litigated, since the deploy decision that reopened this phase supersedes it.

**Goal.** Move media out of the API host's filesystem and into an S3-compatible bucket, so storage stops being tied to one machine's disk.

**Why now, ahead of Phase 6/7.** Not because a single box has stopped working — it hasn't, and on EBS local disk is perfectly serviceable. It's sequencing: Phase 7 builds the download and preview UI against whatever `GET /artifacts` returns, and this phase changes that from *streamed bytes* to a *redirect*. Doing it afterwards means reworking frontend code written days earlier. Ten minutes of ordering now avoids that.

**Why R2 specifically (superseded — kept for context).** Egress, which is the cost that actually scales for video: storage is pennies, but serving a 100MB file 500 times is 50GB of transfer. R2 charges **nothing** for egress and includes 10GB of storage free; S3 charges roughly $0.09/GB out. R2 speaks the S3 API, so `boto3` covers R2, Backblaze B2 and MinIO alike — this is not a lock-in decision, and `S3Storage` still targets any of them via `storage_s3_endpoint_url` (left `None` for real AWS).

**What actually shipped.** Exactly as scoped by the `Storage` ABC — nothing outside `backend/storage/` parses or constructs a URI, so this was purely additive.

- **`backend/storage/s3.py`**: `S3Storage` implementing every `Storage` method (`put`/`put_file`/`get`/`delete`/`exists`/`size`/`open_stream`) against any S3-compatible endpoint via `boto3`, plus `presigned_url()`. `Config(signature_version="s3v4")` set unconditionally — correct for S3, required for R2, harmless everywhere.
- **`backend/storage/base.py`** gained `generate_key()` and `is_safe_key()`, factored out of `LocalDiskStorage` so both backends mint and validate keys identically — a key is always a uuid4 hex plus an optional extension, and anything containing a path separator or a relative segment (`.`, `..`, empty) is rejected before it reaches the filesystem or an S3 call. `presigned_url()` also moved onto the base class as a non-abstract method defaulting to `None`, so `LocalDiskStorage` needed no changes at all.
- **`get_storage()` selects on `Settings.storage_backend`** (`"local"` default, `"s3"` to switch). `boto3` is imported lazily inside the `"s3"` branch so every local/test environment never pays for importing it.
- **`GET /artifacts` returns a 307 (`Cache-Control: no-store`) to a presigned URL** when `storage.presigned_url(uri)` is non-`None`, before ever calling `size()`/`open_stream()`. Local disk keeps streaming exactly as before. The redirect check sits *after* `require_artifact_access` resolves (it's a FastAPI `Depends`, resolved before the route body runs) — a caller who fails the membership/ownership check gets 404, never a redirect, regardless of which backend is active.

**The security detail that was the risk.** `GET /artifacts` deliberately does not re-check path traversal itself; it delegates to the backend's own key guard. `S3Storage._key_for` mirrors `LocalDiskStorage._path_for` via the shared `is_safe_key()`, so a hostile or garbled `uri` is rejected the same way — `S3Storage`, sharing that logic, closes the hole rather than reopening it.

**Migration decision, made explicit rather than left implicit.** Flipping `STORAGE_BACKEND=s3` does **not** migrate anything — every existing `local://` URI (`videos.storage_uri`, `results.artifact_uri`, every asset inside `results.payload`, every `video_assets.uri`) becomes unresolvable the moment the backend switches. The chosen path is a **fresh EC2 box with a fresh, empty database** — true for this deployment since it's a new box, not a resurrection of the terminated one ([[ec2-deployment-reference]]) — so there is nothing to carry over. No copy-and-rewrite script was written; if a future deploy ever needs to switch backends against a database with real rows in it, that script would need writing then, not assumed away.

**Testing.** `moto[s3]` (dev dependency) mocks AWS in-process — no MinIO container, no real account, no network access needed. `tests/test_s3_storage.py` covers the full `Storage` contract plus the same hostile-URI list `test_artifacts_api.py` uses for `LocalDiskStorage` (`test_traversal_and_foreign_uris_are_rejected`), proving both backends refuse a garbled/malicious key identically. `tests/test_artifacts_api.py` gained two tests using a `presigned_url()`-only fake `Storage`: one confirming the 307 + `Cache-Control: no-store`, one confirming a stranger still gets 404 rather than a redirect.

**Worker changes.** None. **Database changes.** None — URIs stay opaque strings in the same columns, now `s3://<key>` instead of `local://<key>` once the backend is switched.

**Not yet done, deliberately deferred:** the API and every worker still need `STORAGE_BACKEND=s3` plus real bucket/credential settings at actual EC2 deploy time — nothing local should switch until the bucket exists. `storage_backend` defaults to `"local"` for exactly this reason.

---

### Phase 6 — Memory / preference loop

**Goal.** Close the loop described in §7/§17: preferences are actually read before planning (already true since Phase 2/4) and actually written after a successful real edit — via an explicit action, not a side effect hidden inside a read.

**Why before Phase 7.** Frontend integration is more meaningful once there's an observable "the assistant remembered my platform preference" behavior to show, not just a working pipeline.

**Components.** A new endpoint, `POST /jobs/{id}/update-memory`, called explicitly by the frontend once it observes (via its normal `GET /jobs/{id}` poll) that the job has reached `completed`. The endpoint:
1. Loads the completed job's conversation.
2. Runs the one lightweight LLM call asking whether any durable preference should be saved.
3. Upserts `user_preferences` if the call says so.
4. Returns immediately once done.

`GET /jobs/{id}` itself is untouched — still purely read-only, no hidden side effects. This is a deliberate, explicit design choice: the client decides when to trigger memory processing, rather than it being an incidental consequence of polling.

**Database changes.** A flag to make the endpoint idempotent — e.g. `conversations.memory_processed_at` (nullable timestamp). On a call, if it's already set, the endpoint returns immediately without re-running the LLM call or re-upserting preferences; if not, it does the work and sets it. Calling the endpoint five times in a row is safe and cheap after the first.

**API changes.** New `POST /jobs/{id}/update-memory` — synchronous, idempotent, no request body needed (or an empty one). Returns success once the check/update is done (or immediately, no-op, if already processed). `GET /jobs/{id}` unchanged.

**Worker changes.** None.

**Frontend changes.** After a poll shows `status: completed`, the frontend calls this endpoint once (and it's safe if called more than once — double-poll, retry, multiple tabs, etc. all land on the same idempotent outcome).

**Testing strategy.** Complete a job; call the endpoint, assert `user_preferences` reflects a plausible update from a scripted conversation and `memory_processed_at` is now set; call it again, assert no second LLM call happens and the response still succeeds (idempotency).

**Demo scenario.** Tell the assistant "always export for LinkedIn" during a session, complete an edit, call `update-memory` explicitly, start a *new* conversation about a *different* video, and show the planner already assumes LinkedIn without being told again.

**Risks.** None specific to this design beyond the general LLM-call risk already covered in Phase 4 (handled the same way: local retry/backoff, and if it ultimately fails, the endpoint should still return success for the *job* itself — a missed preference update is a soft failure, not a reason to surface an error to the user). If Setu later gains a real terminal lifecycle event (`job.completed`, §20), this endpoint's *logic* moves unchanged into a background consumer of that event — only the trigger changes, not the contract.

---

### Phase 7 — Frontend integration

**Goal.** A usable UI over everything built so far: upload, chat, proposal preview and confirmation, poll-based progress, and artifact download.

**Why last.** Every piece it depends on (upload, chat, planning, execution, memory) is already independently proven by this point — this phase is wiring, not new backend logic.

**Components.** Project creation + upload widget → `POST /projects`, `POST /projects/{id}/videos`; chat UI → the messages endpoints; a confirm affordance for proposed workflows; a progress view driven by polling `GET /jobs/{id}` (a simple interval poll is enough for V1, per your instruction — no SSE/WebSocket needed), which on observing `completed` calls `POST /jobs/{id}/update-memory` (Phase 6) once.

**Artifacts, plural (revised per v10).** A finished job yields several assets, not "the video" — the rendered output, the `.srt`, the transcript. The UI lists them from `GET /jobs/{id}/artifacts` (Phase 5A) and offers each for download; a downloadable subtitle file is a real feature that costs nothing extra here, since the asset already exists.

**Preview and the iteration loop (revised per v10).** The confirm affordance is paired with a preview one: `POST /projects/{id}/preview-proposal` renders the same proposal fast and low-res, so the loop becomes *propose → preview → adjust in conversation → preview → confirm for real*. Without this the only way to discover a bad crop is to pay for a full render and start a fresh conversation. **Per-stage inspection** is the debugging affordance on top: intermediate artifacts already exist for every stage, so the UI can show *which step* went wrong rather than only the final result — defaulting to the final output, with per-stage available on demand. Render it per asset kind (frame for visual stages, loudness delta for audio, text for transcript) rather than as N videos; stages that pass the video through untouched are detectable by identical URI and should collapse automatically.

**Database changes.** None.

**API changes.** None new — the artifact-listing, download, and preview endpoints this phase consumes all ship in Phase 5A, where they're needed to verify the capabilities themselves.

**Worker changes.** None.

**Frontend changes.** The whole phase.

**Testing strategy.** Manual end-to-end walkthrough of the full spec example (upload → "make this more professional" → LinkedIn → confirm → watch progress → download); basic component tests for the chat/upload widgets if the frontend stack has a test setup already.

**Demo scenario.** The full product demo: a real video, a real conversation, a real confirmed proposal, real progress, a real output file.

**Risks.** Polling interval too aggressive (hammering the API) or too slow (feels unresponsive) — pick something reasonable (2–3s) and revisit only if it's actually a problem.

---

### Phase 8 — Collaborative Project Rooms

**Goal.** Extend the application from a single-user experience into a shared workspace: multiple users join an existing **Project** — already created back in Phase 1, already holding its videos and shared conversation (Phase 2) — and see the same videos, shared conversation, job progress, and completed exports. This phase is collaboration *infrastructure* only, and specifically just **membership**: `Project` is already the aggregate root (v5) and `Conversation`/`Video` are already project-scoped, not video- or user-scoped. What's missing is *who else* is allowed in. The planner behaves exactly as it does today (it simply receives the shared conversation as context, per §4), and no approval workflow or AI consensus logic exists yet; that's Phase 9a.

**Why after Phase 7.** Everything a room member observes — upload, chat, planning, progress, download — already works single-user by the end of Phase 7. This phase multiplexes an existing experience; it doesn't build a new one.

**Posture note (applies from here on).** Phases 0–7 treated "no changes to Setu's core" as a hard rule. From Phase 8 onward it is a *default*: prefer building above Setu, but modify it when there's a genuinely good reason, and document that reason at the point of change. Phase 8 and Phase 9a need no such change; Phase 9b makes the one exception (cancellation).

**Core concept.** A Project already groups its videos and one shared conversation (Phase 1/2, v5). This phase adds the missing piece: members, turning a single-owner project into a genuine multi-user room. Every message already preserves sender (`Message.sender_id`, nullable, null = assistant), timestamp, and ordering — Phase 2 built that in from the start specifically so this phase wouldn't need to touch it.

**Components.** `project_members` model + repository (the only new table — `projects`, `conversations`, `videos` all already exist and are already project-scoped); a room-snapshot endpoint; a WebSocket fanout layer (designed below, implemented as thin as possible).

**Database changes.** One new table:

```
project_members(project_id FK, user_id, role ('owner'|'member'), joined_at,
                PRIMARY KEY (project_id, user_id))
```

`POST /projects` (Phase 1) needs one small addition here: also insert the owner as the first `project_members` row, so a freshly-created project is never memberless. No changes needed to `conversations`, `messages`, or `videos` — all three already carry the `NOT NULL` `project_id`/sender attribution this phase needs (v5).

- **`jobs` stays untouched.** Room↔job linkage lives in a small `project_jobs(project_id, job_id, submitted_by_user_id)` mapping table written at submission time by the (non-Setu) submission wrapper. `submitted_by_user_id` is whoever's request triggered `JobSubmissionService.submit()` — in this phase, whoever called `confirm-proposal`; from Phase 9a on, whichever member's action satisfied the room's approval policy. This column *is* **job ownership** (Phase 9b's `POST /jobs/{id}/cancel` authorizes directly against it) — captured here, at the point a job is first submitted in a room, rather than invented later by 9a's proposal apparatus, so 9b has no real dependency on proposals/approval existing. If the `project_jobs` join ever proves genuinely annoying, a nullable `project_id`/`submitted_by_user_id` pair of columns on `jobs` is the fallback — an acceptable Setu change under the new posture, but not the starting point.
- **Version history is derived, not stored:** the room's completed export jobs (via `project_jobs` → results/artifacts), ordered by completion time, *are* the version list. No new table until deriving it proves insufficient.

**API changes.**
- `POST /projects/{id}/members` — invite a member (owner-only for V1).
- `POST /projects/{id}/join` — accept an invite / join the room.
- `GET /projects/{id}/members` — list members.
- `GET /projects/{id}` — room snapshot: videos, recent messages, active jobs, completed exports. This is also the reconnect/recovery path for the socket below.
- `POST /projects/{id}/videos`, `POST /projects/{id}/messages` / `GET /projects/{id}/messages` **already exist** (Phase 1/2) and need no change — they gain multi-user meaning simply because `project_members` now exists to check against.

All room endpoints enforce membership; non-members get 403/404.

**Real-time synchronization (WebSocket architecture — designed here; implementation stays deliberately thin).**
- One socket per client per room: `WS /projects/{id}/ws` (membership-checked at connect).
- An in-process connection registry keyed by `project_id`. A single API process is the V1 reality; the scale-out path is Redis pub/sub between API processes — an extension of this design, not a redesign.
- Event envelope: `{seq, type, data}` with a per-room monotonic sequence number. Event types: `message.created`, `planner.replied`, `job.updated` (status/stage progress), `export.completed`, `member.joined`.
- **The socket is pure fanout.** Writes never travel over it — REST remains the only write path. Reconnect = refetch the snapshot via `GET /projects/{id}`, then resume the stream; the `seq` lets a client detect gaps.
- Message/member/planner events are emitted directly by the API at write time. Job-progress events need a bridge out of Setu: **V1 is a server-side watcher that polls the jobs table for the room's active jobs and pushes diffs** — the same polling decision already made in §9/§19 Phase 7, just moved server-side so N clients don't each poll. The clean upgrade is backlog item 1 (terminal lifecycle events via the outbox) feeding this same fanout — when that lands, only the producer changes; the envelope and event types don't.

**Worker changes.** None. Execution is exactly as before: every confirmed proposal still becomes a normal Setu job. `JobSubmissionService`, `WorkflowEngine`, `WorkerRunner`, `StageProcessingService`, workers, retry, DLQ, and outbox are all untouched.

**Frontend changes.** Room create/join flow; shared chat with sender names; member list; progress views driven by the room socket instead of per-client polling.

**Testing strategy.** Two simulated users post interleaved messages → both see the identical ordered history, and each message lands on both sockets exactly once. A planner reply is stored once and fanned to all members. A job submitted in the room produces the same `job.updated` sequence on every connected client. A non-member is rejected from every room endpoint and from the socket. Reconnect mid-job → snapshot + resumed stream misses nothing.

**Demo scenario.** Two browsers, one room: user A uploads a video and chats with the planner; user B watches the messages, planner replies, and job progress appear live without refreshing, then downloads the completed export.

**Risks.** The WebSocket layer growing write paths — resist; it stays fanout-only, with the DB as the single source of truth for ordering. Membership checks scattered ad hoc across endpoints — centralize in one dependency/guard from day one.

---

### Phase 9a — Proposals & Approval Workflow

**Goal.** Upgrade the planner from a single-user assistant into a collaborative facilitator that understands a discussion between multiple participants — who said what, when, where they conflict, where they agree — and, instead of a directly-confirmable plan, produces a **Proposal** that must satisfy the room's approval policy before it is compiled (Phase 3 machinery, unchanged) and submitted to Setu. Execution still happens through Setu exactly as before; only the planning layer changes, and the collaboration layer sits entirely above the execution engine. **No Setu-core changes in this sub-phase** — that's isolated in 9b.

**Why after Phase 8.** Needs the sender-attributed shared conversation and the room fanout to broadcast proposals and approvals.

**Planner changes (the only "AI" change).** The prompt now renders an attributed transcript — `Alice (10:41): "Crop vertically."` / `Bob (10:42): "Keep landscape."` — instead of anonymous user turns. The response discriminator's shape is already `{"type": "proposal", ...}` from Phase 2 (§5/§6) — nothing to rename here. What's new in this sub-phase is two additional fields on that same shape: `{"type": "proposal", "summary": ..., "workflow": [...], "reasoning": ..., "discussion_summary": ...}`; `{"type": "message"}` stays as-is and is how the planner facilitates. Facilitation behaviors — detect conflicting requests, detect agreement, summarize the discussion, explain *why* it chose this workflow (the `reasoning` field), and ask for clarification when needed — are **prompt-level capabilities, not code**. In the Alice/Bob example above, the planner must recognize the conflict and return a clarifying `message` to the team, not a proposal. The Phase 3 validator still gates every proposal's `workflow` exactly as before — nothing about multi-user input loosens validation.

The prompt also renders the room's **active approval policy** as context, and is expected to phrase its facilitation messages accordingly — e.g. "The proposal is ready for admin approval." under `admin`, vs "The proposal is ready. Waiting for approval from all team members." under `team`. This is wording only: the planner never decides *whether* a proposal is approved (that's the collaboration layer, below) — it only speaks about the policy that already governs the room. The planner **never executes a workflow directly**; it only ever produces proposals (or messages). The collaboration layer alone is responsible for collecting approvals and submitting a proposal to Setu once the room's approval policy is satisfied.

**Proposal lifecycle.** `pending → approved → submitted` (with the resulting `job_id` recorded), or `pending → rejected`. A proposal stays `pending` until the room's approval policy is satisfied. **Rejection under `team`** is decided the moment any single member rejects — since every active member must approve, one rejection makes unanimity impossible, so the proposal moves straight to `rejected` without waiting for the rest of the room to vote (`is_rejected(policy, member_count, approvals)`, the mirror of `is_satisfied`, below). A `rejected` proposal does **not** block the room: the conversation stays open, the planner sees the rejection in the transcript, and — if the discussion converges on something new — produces a fresh `proposal`-type response, which is a new `proposals` row (proposals are never mutated after rejection; the old row stays as an audit record of what was rejected and why). Approval collection and policy evaluation are plain application logic — no LLM involvement past proposal generation.

**Approval policies.** A policy is an enum on the project plus one pure function `is_satisfied(policy, member_count, approvals)` (and its mirror `is_rejected`, above) — **not** a table, so additional policies are new branches of the same two functions, never a schema change. V1 ships two:

- **`admin`** ("Admin Mode") — one designated approver's decision is final; other members' votes, if any, don't count toward the outcome. Suits a lead-driven workflow where one person signs off.
- **`team`** ("Team Mode", **the V1 default**) — every *active* project member must approve before execution begins; any single rejection ends the proposal (see lifecycle, above). Suits small creative teams making decisions collectively — the framing this default is chosen for.

**Open question to settle when this phase starts (raised in v10, deliberately not decided now).** `team` as the default may be real friction once Phase 5's preview/iteration loop exists: unanimous approval is right for a final render, but requiring all members to sign off on every exploratory tweak would grind. Two candidate resolutions, neither committed to here — default to `admin` instead, or scope the policy per *action* (previews unapproved and free, real renders governed by policy) rather than per room. The second is more expressive and fits the preview model directly, but adds a dimension to policy evaluation that `is_satisfied`/`is_rejected` don't currently have. Decide with the preview loop actually in hand rather than speculatively.

Note this `admin` policy value is deliberately distinct from **job ownership** (Phase 8's `project_jobs.submitted_by_user_id`, used by Phase 9b) — a proposal's admin-approver need not be the same person as the job's owner; conflating the two names was a V1-draft mistake, fixed here before either ships. When an approval-triggered submission happens, `proposals.created_by_user_id` is the value written into `project_jobs.submitted_by_user_id` — 9a populates an existing Phase-8 column, it doesn't invent the ownership concept.

**Future policies (backlog, not V1).** Majority Approval, Selected Reviewers, Two-Level Approval (Lead + Manager), Custom Approval Rules — see backlog item 5 in §20. `admin`/`team` need only the existing `proposal_approvals` vote rows; Selected Reviewers and Two-Level Approval will likely need to know *which* members are eligible to vote at all, which `admin`/`team` don't — that's new data (a reviewer set or role), not just a new enum branch, so it's deferred rather than stubbed in now.

**Database changes.** Lightweight:

```
proposals(id, project_id FK, created_by_user_id, summary, reasoning,
          discussion_summary, workflow_json, status
          ('pending'|'approved'|'rejected'|'submitted'),
          job_id FK nullable, created_at, updated_at)

proposal_approvals(proposal_id FK, user_id, decision ('approve'|'reject'),
                   created_at, PRIMARY KEY (proposal_id, user_id))

projects.approval_policy ('admin'|'team') NOT NULL DEFAULT 'team'
```

One vote row per member per proposal; a member may change their vote while the proposal is still `pending`.

**API changes.**
- Proposal *creation* is the planner's job: a `proposal`-shaped planner response is persisted as a `proposals` row and fanned out. No manual-creation endpoint in V1.
- `GET /projects/{id}/proposals` — list (filterable by status).
- `GET /proposals/{id}` — full proposal including current approval status/progress (who has voted, what the policy requires).
- `POST /proposals/{id}/approve` / `POST /proposals/{id}/reject` — member-only. Each approval triggers policy evaluation **inside one transaction with a status guard** (only one `pending → submitted` transition can ever win, and only one `pending → rejected` transition can ever win), then: if satisfied, validate + compile via Phase 3 unchanged → `JobSubmissionService.submit()` → record `job_id`, mark `submitted`; if a `team` proposal just became impossible to satisfy, mark `rejected`.
- Phase 8's socket gains `proposal.created` and `proposal.updated` event types — same envelope.

**Worker changes.** None.

**Frontend changes.** Proposal card (summary, reasoning, discussion summary, workflow steps, vote buttons, approval progress including who under `team` has yet to vote); rejected proposals shown inline in the room history, not hidden.

**Testing strategy.** Prompt-level: a conflicting transcript (Alice vs Bob above) → clarifying message, no proposal; an agreeing transcript → proposal with sane `reasoning`/`discussion_summary`; a policy-aware facilitation message matches the room's active policy. Unit: `is_satisfied`/`is_rejected` across `admin`/`team` including edge counts (single member, member leaves mid-vote). Integration: `team` approvals below unanimity → nothing submitted; last member approves → exactly one Setu job submitted even under simultaneous approvals (the transaction/status-guard race test); any single `team` reject → proposal `rejected` immediately without waiting on remaining votes, room conversation stays open, a later planner turn produces a new proposal row.

**Demo scenario.** Three-user room, `team` policy (the default). Alice: "Crop vertically." Bob: "Keep landscape." The planner names the conflict and asks the team to decide. They settle on 9:16; the planner produces a proposal with reasoning and a discussion summary, and tells the room it's "waiting for approval from all team members." Bob initially rejects it wanting a different crop; the proposal is marked `rejected` immediately, discussion continues, and the planner issues a revised proposal once the room agrees. All three members approve the revised proposal → a real Setu job submits and runs while the room keeps chatting about the next edit.

**Risks.** Consensus logic creeping into code — conflict/agreement detection lives in the prompt; code only enforces policy arithmetic. Double-submit on racing approvals — covered by the transactional status guard, and tested. Facilitation quality is prompt iteration, not schema iteration — resist adding tables to fix a prompt problem. `team` policy's reject-and-regenerate loop could churn on a room that can't agree — acceptable for V1 (it mirrors real disagreement), not something to solve with code.

---

### Phase 9b — Cooperative Cancellation

**Goal.** Let the job owner stop a running job between stages. The only sub-phase in the entire roadmap that touches Setu's core, isolated here on purpose so the one exception is reviewable, testable, and demoable on its own — independent of whether 9a's approval mechanics are even in place.

**Why sequenced here, not strictly after 9a.** Its only real prerequisite is a submitted job with a recorded owner — and that's `project_jobs.submitted_by_user_id`, captured as early as Phase 8 (see Phase 8's Database changes), not anything from 9a's proposal/approval machinery. This sub-phase is listed after 9a purely because both are grouped under "Phase 9" as the collaboration-and-execution-control bundle for review purposes (per Changelog v4's reasoning for the 9a/9b split) — it could ship right after Phase 8, or in parallel with 9a, without missing anything.

**Task ownership + cancellation.** The **job owner** is whoever's action triggered submission, recorded in `project_jobs.submitted_by_user_id` as early as Phase 8 — not invented here. In the single-owner world (Phase 4/8) that's trivially whoever called `confirm-proposal`; once 9a's approval workflow exists, it's the proposal's creator (`proposals.created_by_user_id`), copied into that same column when approval-triggered submission happens. Job ownership is unrelated to the `admin` approval policy from 9a; only the job owner can cancel the running job. Other members keep discussing future edits while the job runs — the room conversation is never blocked by execution. Ownership is queryable from `project_jobs.submitted_by_user_id` directly — no dependency on the `proposals` table at all, so cancellation could in principle ship before 9a if ever prioritized that way. Cancellation is **this sub-phase's one justified Setu change** (backlog item 3, pulled forward): `JobStatus.CANCELLED` exists but is unreachable today. Minimal mechanic — *cooperative cancellation*: `POST /jobs/{id}/cancel` (authorized against the job owner) sets the job's status to `cancelled`; `WorkflowEngine` checks the job's status before dispatching each next stage and stops if cancelled. The in-flight stage runs to completion — no mid-stage kills, no worker changes, no retry/DLQ changes. Reason documented per the posture note: cancellation is inherently engine-state, and faking it above Setu (ignoring results of a job that keeps running) would be dishonest state.

**Database changes.** None beyond what Phase 8 already added (`project_jobs.submitted_by_user_id`) — `JobStatus.CANCELLED` is an existing enum value, just newly reachable.

**API changes.** `POST /jobs/{id}/cancel` — job-owner-only.

**Worker changes.** None.

**Frontend changes.** Cancel affordance visible only to the job owner, on the running job's progress view.

**Testing strategy.** Non-owner cancel → 403. Owner cancel mid-workflow → job ends `cancelled`, no further stages dispatched, in-flight stage's result intact. Owner cancel after the job already reached a terminal state → no-op or 409, not a crash.

**Demo scenario.** Continuing from 9a's demo: Alice, as the submitted job's owner, cancels it mid-run; the in-flight stage finishes, no further stage dispatches, and the job shows `cancelled` while the rest of the room keeps working.

**Risks.** Scope creep into hard cancellation (mid-stage kill) — explicitly out of scope; cooperative-only. Engine-state honesty — this is why cancellation lives in `WorkflowEngine` itself rather than being faked one layer up.

---

### Phase 10 — Semantic video understanding (sketched in v10; foundation shipped in v16; `detect_scenes`/`detect_filler_words` shipped in v17 — see that changelog. Diarization, object/face detection and saliency remain unbuilt and unscoped)

**Goal.** Move from *deterministic execution of stated instructions* to *understanding what is actually in the video* — so the assistant can propose edits nobody explicitly asked for: find the highlights, reframe following the speaker, remove filler words, locate "the part where she talks about pricing."

**Why last.** Every prior phase is about faithfully executing an instruction the user (or the room) already articulated. This is the first phase where the system contributes an opinion about content. It needs the real media pipeline (Phase 5) to act on, and the asset model to store what it learns.

**The one structural change it needs: assets become video-scoped, not job-scoped.** Today all analysis output lives in a `Result` row, reachable only via `videos.latest_analysis_job_id`. That works for a single analysis pass but doesn't survive what this phase needs: re-analysis orphans the previous results, and "which videos contain a dog" means scanning JSON blobs across unrelated jobs. The natural evolution is a `video_assets(video_id, kind, uri, data, created_at)` table — the same `Asset` concept from Phase 5A, promoted from *per-run* to *durable per-video*.

Because Phase 5A defines `Asset` as a clean serializable value object rather than burying `{kind, uri}` inline in each worker's dict, that promotion is a new table plus a write — not a refactor of every capability. This is the specific reason the asset model was worth building properly in Phase 5 rather than threading a single `output_uri` string through.

**Immediate payoff before any of the AI work lands:** caching the transcript against the *video* rather than the job makes Phase 5's preview loop genuinely fast. Transcription is the one expensive step that doesn't get cheaper at preview resolution — it runs on audio — so without caching, any preview containing `transcribe` pays the full transcription round-trip (and its per-minute cost) again, and "fast preview" is a lie for exactly the workflows people most want to check. Transcribe once, reuse across previews, re-runs, and final renders. **This is worth pulling forward ahead of the rest of Phase 10** whenever the preview loop starts feeling slow.

**Likely capabilities** (each still a normal capability under Phase 5's model — new asset kinds, not new machinery): scene/shot detection, speaker diarization, object and face detection, filler-word detection, saliency for auto-reframe, and embeddings for semantic search across a project's footage.

**Explicitly still out of scope even here:** none of this changes the planner's single-call, no-multi-agent posture (§5 AI architecture principles). Richer *context* going into the prompt is not the same as an agent framework, and the gate in principle 1 applies with full force at this phase.

---

## 20. Backlog — deferred, not required for V1

Kept separate deliberately, per "avoid over-engineering" — these are real, reasoned improvements, but none are load-bearing for the phases above, so none should be pulled forward without a concrete reason:

1. **Terminal lifecycle event** (`job.completed`/`job.dead_lettered` via the outbox, as in v1 §21) + a real Notification Worker/SSE/webhook — worth it only if polling in Phase 7 turns out to be genuinely too slow or chatty in practice. Still a generically useful Setu improvement whenever it does happen (any future project on this engine would benefit from "job is done" being a pushable event, not just a pollable field) — just not a V1 blocker now that polling is the explicit V1 choice. Phase 8's WebSocket job-progress bridge also starts as a server-side poll of the jobs table; when this event lands, it replaces that poller as the producer feeding the same room fanout — envelope and event types unchanged.
2. **Formalizing the capability registry as a reusable Setu-level concept** (not video-specific) — the registry built in Phase 3 is currently local to this project; promoting it into Setu proper would let any future workflow (human-authored or another LLM planner) discover valid stages the same way. Reasonable later, not needed to ship this.
3. **Cancellation** — **no longer backlog: pulled forward into Phase 9b** (job-owner-only cooperative cancellation between stages, as that sub-phase's one justified Setu change). Kept in this list only so the numbering and the original reasoning remain traceable.
4. **Stabilization / Compression / Thumbnail workers** — same mechanism as Phase 5's workers, just not needed for the core "make this more professional for LinkedIn" flow; add exactly the same way (one class, one registry line) whenever they're wanted.
5. **Additional approval policies** — Majority Approval, Selected Reviewers, Two-Level Approval (Lead + Manager), Custom Approval Rules, beyond Phase 9a's `admin`/`team`. The architecture already supports this cheaply: policies are an enum + a pure `is_satisfied`/`is_rejected` function pair, so each new policy is a new branch, not a schema change or an execution-engine change. Selected Reviewers and Two-Level Approval are the ones that will eventually need real new data (which members are eligible reviewers, or a two-tier role split) rather than just vote-counting logic — worth designing properly when one of these becomes a real requirement, not speculatively now.

6. **Multi-input / multi-output execution — `split`, mid-chain `merge`.** The one genuinely blocked capability family, and the only item here that needs a real execution-model change rather than a new worker. Two concrete blockers: `Result`'s `UniqueConstraint(job_id, stage)` means one Result per stage, so a one-to-many `split` has nowhere to put its outputs; and `StageProcessingService` sources `previous_output` from stage N-1 only, so a mid-chain merge has nowhere to draw multiple non-adjacent inputs from. Phase 5E ships first-stage-only `merge` (which needs neither), and defers the rest here. Lifting this properly means either fan-out/fan-in dispatch in `WorkflowEngine` or a real DAG workflow model — a legitimate project, worth its own architecture pass, and explicitly *not* worth hacking around with a special case that undermines the clean single-input/single-output model everything else relies on.
7. **Partial re-run / resume-from-stage.** Every stage's intermediate artifact already persists, so re-running a workflow after changing only its last capability could reuse the cached stage N-1 output instead of redoing everything upstream. Combined with preview (Phase 5A), this is what would make the iteration loop feel genuinely fast — "re-run the last step" rather than "re-run everything." Needs a cache-validity rule (which params invalidate which downstream stages), which is why it isn't free and isn't in Phase 5.
8. **~~Artifact retention / garbage collection~~ — done.** A background sweeper in the API lifespan (`backend/services/artifact_cleanup_service.py`) drops intermediate artifacts from jobs that have been finished longer than `artifact_retention_hours` (24 by default), keeping the final stage's output. Retention rather than immediate deletion because intermediates are what make "which stage got it wrong" answerable, and a preview is worth re-watching. Three things are never swept, and each would be data loss rather than reclamation: a user's source upload (which genuinely appears in an intermediate Result, because `transcribe` re-encodes nothing and reports its input URI as its output), the final stage's assets, and anything another job still references (preview and confirm jobs share URIs by construction). Partial re-run (item 7) stays open and is now bounded by this window.
9. **DLQ replay tooling.** The `.dlq` topics are currently write-only — nothing consumes them, and Postgres independently holds the whole failure record (`job.status`, `job.last_error`, a `WorkerExecution` row per attempt), with `JOBS_DEAD_LETTERED_TOTAL` incremented from the Postgres path rather than the topic. Reviewed during Phase 5A and **deliberately kept anyway**, for a reason worth recording so it isn't re-litigated: the permanent-vs-transient split is knowingly coarse (`media.run_ffmpeg` classifies *every* nonzero ffmpeg exit as permanent, which sweeps in genuinely transient OOM/disk-full cases), and the DLQ is the backstop for that misclassification. Phase 5 adds many more such coarse judgments, so the safety net matters more than it did in Phases 0–4, not less. What's missing is a small `replay-dlq <topic>` command re-producing messages to the source topic — worth building once real users can have real dead-lettered jobs, i.e. after Phase 7, not before. Also note the cost side: every stage topic gets a `.dlq` sibling, so the now-14 media capabilities (Phase 5's 13 plus `remove_segment`, changelog v19) mean ~28 topics against a Redpanda instance running `--memory=1G --smp=1` (its 256-partition cap already bit once, via accumulated test topics).
11. **~~Object storage for media~~ — shipped as Phase 5H (§19) against Amazon S3, 2026-08-02.** `S3Storage` exists behind the same `Storage` ABC; `storage_backend` still defaults to `"local"` until the EC2 deploy actually flips it. Kept in this list so the numbering and the original reasoning stay traceable; §19 Phase 5H has the full write-up, including the R2-to-S3 switch and why it happened.
12. **Capability filtering at scale.** The planner prompt renders every registered capability with its param schema. Fine at Phase 5's ~10; somewhere past ~30, prompt size and stage-selection accuracy both degrade. The fix when it comes is capability retrieval/filtering — narrowing what's offered per request — and explicitly **not** a multi-agent decomposition, which §5's principles rule out. Noted now so the ceiling is known rather than discovered.

None of these require restructuring anything built in Phases 0–7 — that's the point of listing them here instead of folding them in early. Item 6 is the sole exception and is scoped accordingly: it changes the execution model itself, which is precisely why it's deferred to its own pass rather than absorbed into a capability sub-phase.

---

This is architecture and planning only — no code was written or modified, and no implementation was done.
