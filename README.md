# VedAI

An AI agent for teams, not a tool for one person. You open a room, upload
footage, and describe the edit you want in chat — the agent turns that into a
concrete plan, the room reviews and approves it together, then it actually
executes. No timeline. No toolbar. No drag-and-drop. The only path to an edit
is chat → proposal → approval → job, on purpose — so nothing changes in the
video that everyone in the room didn't see and agree to first.

This repo holds both halves:

- [`backend/`](backend) — **Setu**, the event-driven engine and the LLM agent
  that turns chat into validated, executable workflows.
- [`frontend/`](frontend) — **vedai-studio**, the room: chat, proposal review,
  job progress, and the finished render.

Each has its own README with setup and run instructions.
