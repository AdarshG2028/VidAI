# VedAI Studio

Frontend for [VedAI](https://github.com/AdarshG2028/Setu) — a collaborative video
editor you drive by conversation rather than by timeline.

You create a room, upload footage, and describe the edit you want in chat. An LLM
planner turns that into a concrete workflow, the room votes on it, and the backend
executes it. This app is the room: the conversation, the plan under review, the
job running, and the finished render.

**There is no timeline and no toolbar, by design.** The backend exposes no
direct-edit endpoint — the only path to an edit is chat → proposal → approval →
job. The fifteen capabilities the planner can reach for (trim, crop, merge,
transcribe, burn subtitles, scene detection, …) are shown on the landing page as
what the AI can do, never as clickable tools.

## Stack

TanStack Start (React 19, Vite) · TypeScript · Tailwind v4 · shadcn/Radix
primitives · **bun** as the package manager and runtime.

## Running it

The backend must be running first — see its
[README](https://github.com/AdarshG2028/Setu) for that.

```bash
bun install

# Point at the backend. Defaults to http://localhost:8000 if unset.
echo "VITE_API_BASE_URL=http://localhost:8000" > .env.local

bun run dev          # http://localhost:8080
```

Other scripts: `bun run build`, `bun run preview`, `bun run lint`,
`bun run format`.

## Pages

| Route | What it is |
| --- | --- |
| `/` | Landing page — what the product is, and the capability list |
| `/rooms` | Dashboard — create a room, join one by id, resume a recent one |
| `/rooms/:id` | The workspace — chat, members, footage, proposals, jobs, exports |

## How it talks to the backend

**Identity is a browser-generated UUID.** There is no signup, no login and no
users table anywhere in the system — the backend reads an asserted `X-User-Id`
header and believes it. This app generates one on first load
(`src/lib/identity.ts`), keeps it in `localStorage`, and sends it on every
request. Two places can't send a header — a `<video src>` and a `WebSocket` URL —
so those carry the same id as a `user_id` query parameter instead.

Because there's no account, **getting invited means sending someone your id**.
It's copyable from the dashboard header, the room header avatar, and the members
panel.

**Room state is a snapshot plus a live stream.** `src/hooks/useRoom.ts`
implements the backend's reconnect protocol exactly: open the WebSocket first,
buffer what arrives, fetch `GET /projects/{id}`, discard any buffered event at or
below the snapshot's `seq`, then go live. Close code `1008` means not a member
(don't retry), `1013` means the client fell behind (refetch the snapshot), and
anything else reconnects with backoff.

**Members have no display names**, so `src/lib/memberDisplay.ts` derives a stable
adjective-noun label and colour from the raw UUID by hash. Every client computes
the same one independently, which keeps a room of UUIDs readable with no backend
change.

### Two cross-origin details worth knowing

The app and the API are on different origins in every environment, which breaks
two things that look like they should work:

- **`download_url` from the API is relative** and carries no identity, so it
  resolves against this app's origin and 404s. Always build artifact URLs with
  `artifactUrl()` in `src/lib/api.ts` — the `Artifact` type deliberately doesn't
  expose `download_url`, so TypeScript catches any attempt to use it.
- **`<a download>` is ignored across origins** — the browser navigates instead of
  saving. `downloadFile()` fetches the bytes and saves them from a same-origin
  `blob:` URL.

## Known gaps

These are backend limitations the UI works around rather than bugs here:

- **No remove-member endpoint** — the control is present but disabled.
- **No list-my-projects endpoint** — the dashboard tracks rooms in
  `localStorage`, so it's per-device.
