# VedAI Studio

# Build: VedAI — AI Conversational Video Editor (frontend only)

## What this product is

VedAI is a collaborative, AI-driven video editor. Users create a "room" (project),

upload videos, and describe edits in a chat interface. An AI planner proposes a

structured edit workflow; room members vote to approve it; once approved, the

backend executes it and the finished video appears. This is NOT a timeline/

drag-and-drop editor — there is no manual crop/trim UI. All editing happens

through conversation. The frontend's job is to make that conversation, the

review-and-approve step, and the resulting media feel excellent.

## Visual direction — this is the most important part of this brief

Build something that looks like a premium creative tool, not a generic

CRUD-app scaffold. Reference points for tone (don't clone, just match the

caliber): Linear, Arc browser, Runway ML, Descript. Specifically:

- **Dark-mode-first.** A deep near-black background (not pure #000 — something

  with a faint cool or violet undertone), with layered surface elevations

  (cards sit on a slightly lighter plane than the page background, with a

  soft 1px border at low opacity rather than a hard line).

- **One vivid accent, used sparingly.** A gradient accent (e.g. violet →

  cyan, or amber → rose — pick one coherent direction and commit to it) used

  only for primary actions, active states, and small highlight details —

  never as a dominant background fill. Everything else stays neutral/dark

  so the accent actually reads as an accent.

- **Typography with real character.** A distinctive display/heading font

  (something geometric or slightly editorial — not the default system

  sans) paired with a clean, highly legible body font. Generous line-height.

  Real type-scale hierarchy, not three sizes of the same weight.

- **Depth via soft glow and blur, not drop shadows.** Subtle glassmorphism

  on floating surfaces (modals, the chat input bar, popovers) — background

  blur + a faint gradient border — rather than flat Material-style shadows.

- **Motion.** Smooth, physics-based transitions (200–300ms, ease-out) on

  every state change: message arriving, proposal appearing, a vote

  registering, a job progressing. Nothing should just "pop" into place.

- **Real empty states and loading states**, illustrated/animated, not blank

  screens or spinners alone — a room with no videos yet, a chat with no

  messages yet, a job that's rendering, should each feel considered.

- **Generous whitespace and breathing room** — avoid cramming; let the

  chat and the video preview both have room to breathe.

The bar: someone should look at this and assume it's a funded startup's

product, not an AI-generated CRUD scaffold.

## Tech expectations

React + TypeScript + Tailwind (shadcn/ui is welcome for base primitives,

but restyle everything to match the direction above — don't ship default

shadcn styling). Backend API base URL must be a configurable env var

(`VITE_API_BASE_URL` or equivalent), defaulting to `http://localhost:8000`.

## Identity — there is no login

The backend has no users table and no authentication. Identity is a

client-generated UUID:

- On first load, generate one with `crypto.randomUUID()` and persist it in

  `localStorage`.

- Send it as an `X-User-Id` header on every API request.

- The **two exceptions** that can't carry custom headers — the artifact

  download URL used in a `<video src>`, and the room WebSocket URL — must

  instead append it as a `?user_id=<uuid>` query parameter.

## CORS

Already permissive on the backend (`Access-Control-Allow-Origin: *`), no

special handling needed.

---

## Pages required

### 1. Landing page

Marketing-quality, not a placeholder. Explain the product (chat-driven AI

video editing, collaborative rooms), show the capability list as a feature

grid — the AI can currently reach for: **crop, resize, rotate, flip, pad,

color adjustment, audio normalize/silence-removal, trim, remove a middle

segment and rejoin the rest, merge multiple clips, transcribe + burn

subtitles, detect scene cuts, detect filler words (um/uh), and render the

final output.** Present these as "what the AI can do," not as clickable

buttons — there's no direct-manipulation editing UI. A clear CTA into the

dashboard.

### 2. Dashboard (`/rooms` or similar)

Create a new room, or join an existing one by ID. **Important backend gap**:

there is no "list my rooms" endpoint — track room IDs the user created or

joined in `localStorage` and re-fetch each one's current state to render

the dashboard list.

### 3. Room workspace (`/rooms/:id`)

The core screen. Needs, all visible at once or via clean tabs/panels — your

call on layout, but everything must be reachable without excessive

navigation:

- **Chat** (see spec below) — the primary interaction surface.

- **Members** — list of room members (raw UUIDs; see display-name note

  below), an invite-by-UUID control (owner-only — see API), and each

  member's role.

- **Video library** — uploaded videos in this room, upload control,

  per-video status (analyzing/analyzed/failed).

- **Proposals** — the AI's pending/past proposed workflows, each showing

  its stage list, a plain-language summary, reasoning, discussion summary

  (when multiple participants are in the room), and approve/reject

  controls reflecting the room's approval policy (`admin` = one approver

  decides; `team` = every active member must approve, any single reject

  ends it immediately).

- **Jobs / progress** — active and recently-ended jobs with live status.

- **Exports** — finished renders, playable inline, downloadable.

### Member display names

The backend has no display names — members are raw UUIDs. Derive a stable,

readable label (e.g. an adjective-noun pair) and a stable color **purely

from the UUID via a deterministic hash**, computed independently by every

client, so every viewer sees the same label/color for the same person

without any server round-trip or shared state.

---

## Chat specification (exact)

- **Your own messages**: right-aligned, one consistent color/style for you.

- **Every other member's messages**: left-aligned, each member gets their

  own distinct color (from the deterministic UUID hash above).

- **AI planner messages**: left-aligned, a clearly distinct "AI" color/style

  separate from any human member's color — should read unmistakably as

  "the assistant," not as another participant.

- When a message is a **proposal** (`type: "proposal"` in the planner

  response), render it as a distinct card, not a plain chat bubble — show

  the workflow's stage list, summary, and the approve/reject controls

  inline.

---

## Real-time updates — WebSocket protocol (implement exactly this)

`WS /projects/{project_id}/ws?user_id=<uuid>`

Every message is an envelope: `{"seq": number, "type": string, "data": {...}}`.

Event types you'll receive: `message.created`, `planner.replied`,

`proposal.created`, `proposal.updated`, `member.joined`, `job.updated`,

`export.completed`.

**Correct connect sequence (important — get this right or you'll get race

conditions):**

1. Open the WebSocket **first**.

2. Buffer every event that arrives before step 3 completes.

3. Fetch `GET /projects/{id}` (the full room snapshot, includes a `seq`

   field).

4. Discard every buffered event whose `seq <= snapshot.seq`.

5. Render the snapshot, then apply remaining buffered events in order, then

   go live — apply new events as they arrive.

**Close codes:**

- `1008` = you're not a member, or the room doesn't exist — do not retry,

  show an error.

- `1013` = you fell behind the server's buffer — refetch the snapshot (back

  to step 3) before resuming.

- Anything else (e.g. `1001` server shutdown) — reconnect with exponential

  backoff.

---

## Full backend API reference

Base: configurable, default `http://localhost:8000`. All authenticated

requests need `X-User-Id` (header) or `?user_id=` (query, for the two

exceptions above).

**Projects (rooms)**

- `POST /projects` — body `{name?: string}` → `{id, owner_id, name, approval_policy, created_at}`

- `PATCH /projects/{id}` — owner-only, body `{approval_policy: "admin"|"team"}` → same shape

- `GET /projects/{id}` — full room snapshot: `{seq, project, members[], videos[], messages[], active_jobs[], ended_jobs[], exports[]}`

- `POST /projects/{id}/messages` — body `{content: string}` → `{message_id, response: {type:"message", text} | {type:"proposal", summary, workflow}}`

- `GET /projects/{id}/messages` — → `{messages: [{id, role, sender_id, content, created_at}]}`

- `GET /projects/{id}/videos` — → `{videos: [{id, original_filename, name, created_at}]}`

- `POST /projects/{id}/videos` — multipart: `file`, optional `name` → `{video_id, project_id, job_id, name, status}`. Returns **409** if `name` collides with an existing video in the room.

- `PATCH /projects/{id}/videos/{video_id}` — body `{name: string}` → renames, returns the video summary

- `POST /projects/{id}/preview-proposal` — low-res preview run

- `POST /projects/{id}/members` — owner-only, body `{user_id: uuid}` → invites

- `POST /projects/{id}/join` — join an existing room

- `GET /projects/{id}/members` — → `{members: [{user_id, role, joined_at}]}`

- `WS /projects/{id}/ws` — see protocol above

**Videos**

- `GET /videos/{id}` — → `{id, project_id, original_filename, name, status, analysis, created_at}` (`status`: analyzing/analyzed/failed; no raw-video-preview URL — see gaps below)

**Proposals**

- `GET /projects/{id}/proposals` — → `{proposals: [...]}`

- `GET /proposals/{id}` — → proposal + `{approval: {policy, approved_by[], rejected_by[], awaiting[], required}}`

- `POST /proposals/{id}/approve` — → `{proposal, job_id?}` (`job_id` set only if this vote triggered execution)

- `POST /proposals/{id}/reject` — → same shape

**Jobs**

- `GET /jobs/{id}` — → `{id, status, workflow[], current_stage, total_stages, attempts, max_attempts, created_at}`

- `GET /jobs/{id}/artifacts` — → `{job_id, status, stages: [{stage, worker_name, artifacts: [{kind, uri, download_url}]}]}`

- `POST /jobs/{id}/cancel` — job-owner-only

**Artifacts**

- `GET /artifacts?uri=...&job_id=...` — streams the file, Range-request aware (use directly as a `<video src>`, appending `&user_id=<uuid>` since headers aren't available there)

---

## Known backend gaps — sections that need workarounds, not endpoints that don't exist

- **No remove-member endpoint.** Don't build a "remove member" button that

  calls anything — there's nothing to call. If you want the affordance

  present, show it disabled with a "not available yet" tooltip rather than

  wiring it to a 404.

- **No list-my-rooms endpoint.** The dashboard must be built entirely on

  `localStorage`-tracked room IDs, refetched individually — there is no

  server-side query for "rooms this user belongs to."

- **No raw-video-preview endpoint.** `GET /videos/{id}` has no playable URI

  for the original upload — only finished job artifacts (via

  `/jobs/{id}/artifacts`) are playable. Don't build a "preview original

  upload" player; only rendered exports are watchable.

## Deliverable

A working multi-page app wired against the real API above (not mocked),

handling loading/error/empty states throughout, with the WebSocket protocol

implemented exactly as specified — and, above all, visually distinctive

enough that it doesn't read as an AI-scaffolded CRUD app.

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/b75b923a-0a67-40de-85c4-6be3c45447bc).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```
