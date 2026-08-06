import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  roomSocketUrl,
  type ChatMessage,
  type ExportItem,
  type Job,
  type Member,
  type Proposal,
  type Snapshot,
  type VideoSummary,
} from "@/lib/api";

export type WsEnvelope = { seq: number; type: string; data: Record<string, unknown> };

export type ConnectionStatus =
  | "connecting"
  | "live"
  | "reconnecting"
  | "resyncing"
  | "denied"
  | "offline";

export type RoomState = Snapshot & { proposals: Proposal[] };

const TERMINAL = ["succeeded", "failed", "cancelled", "canceled", "error", "completed"];

function upsert<T>(list: T[], item: T, key: (x: T) => string | undefined): T[] {
  const id = key(item);
  const idx = list.findIndex((x) => key(x) === id);
  if (idx === -1) return [...list, item];
  const next = [...list];
  next[idx] = { ...(list[idx] as object), ...(item as object) } as T;
  return next;
}

function applyEvent(state: RoomState, evt: WsEnvelope): RoomState {
  const data = (evt.data ?? {}) as Record<string, unknown>;
  const next: RoomState = { ...state, seq: Math.max(state.seq, evt.seq ?? state.seq) };

  switch (evt.type) {
    case "message.created":
    case "planner.replied": {
      const raw = (data["message"] ?? data) as ChatMessage;
      if (!raw || typeof raw !== "object" || !("content" in raw)) return next;
      next.messages = upsert(state.messages, raw, (m) => m.id);
      return next;
    }
    case "proposal.created":
    case "proposal.updated": {
      const raw = (data["proposal"] ?? data) as Proposal;
      if (!raw?.id) return next;
      next.proposals = upsert(state.proposals, raw, (p) => p.id);
      return next;
    }
    case "member.joined": {
      const raw = (data["member"] ?? data) as Member;
      if (!raw?.user_id) return next;
      next.members = upsert(state.members, raw, (m) => m.user_id);
      return next;
    }
    case "job.updated": {
      const raw = (data["job"] ?? data) as Job;
      if (!raw?.id) return next;
      const ended = TERMINAL.includes(String(raw.status ?? "").toLowerCase());
      if (ended) {
        next.active_jobs = state.active_jobs.filter((j) => j.id !== raw.id);
        next.ended_jobs = upsert(state.ended_jobs, raw, (j) => j.id);
      } else {
        next.ended_jobs = state.ended_jobs.filter((j) => j.id !== raw.id);
        next.active_jobs = upsert(state.active_jobs, raw, (j) => j.id);
      }
      return next;
    }
    case "export.completed": {
      const raw = (data["export"] ?? data) as ExportItem;
      next.exports = upsert(
        state.exports,
        raw,
        (e) => (e.id ?? e.uri ?? e.job_id) as string | undefined,
      );
      return next;
    }
    case "video.updated":
    case "video.created": {
      const raw = (data["video"] ?? data) as VideoSummary;
      if (!raw?.id) return next;
      next.videos = upsert(state.videos, raw, (v) => v.id);
      return next;
    }
    default:
      return next;
  }
}

export function useRoom(projectId: string) {
  const [state, setState] = useState<RoomState | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [error, setError] = useState<string | null>(null);

  const stateRef = useRef<RoomState | null>(null);
  const bufferRef = useRef<WsEnvelope[]>([]);
  const liveRef = useRef(false);
  const socketRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disposedRef = useRef(false);

  const commit = useCallback((updater: (s: RoomState) => RoomState) => {
    if (!stateRef.current) return;
    const next = updater(stateRef.current);
    stateRef.current = next;
    setState(next);
  }, []);

  /** Steps 3–5: fetch snapshot, drop stale buffered events, apply the rest, go live. */
  const syncSnapshot = useCallback(async () => {
    setStatus((s) => (s === "live" ? "resyncing" : s));
    try {
      const [snapshot, proposalsRes] = await Promise.all([
        api.getSnapshot(projectId),
        api.getProposals(projectId).catch(() => ({ proposals: [] as Proposal[] })),
      ]);
      let base: RoomState = {
        ...snapshot,
        members: snapshot.members ?? [],
        videos: snapshot.videos ?? [],
        messages: snapshot.messages ?? [],
        active_jobs: snapshot.active_jobs ?? [],
        ended_jobs: snapshot.ended_jobs ?? [],
        exports: snapshot.exports ?? [],
        proposals: proposalsRes.proposals ?? snapshot.proposals ?? [],
      };
      const pending = bufferRef.current
        .filter((e) => (e.seq ?? 0) > snapshot.seq)
        .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
      bufferRef.current = [];
      for (const evt of pending) base = applyEvent(base, evt);
      stateRef.current = base;
      setState(base);
      setError(null);
      liveRef.current = true;
      if (socketRef.current?.readyState === WebSocket.OPEN) setStatus("live");
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load room");
      if (!stateRef.current) setStatus("offline");
      return false;
    }
  }, [projectId]);

  const connect = useCallback(() => {
    if (disposedRef.current || typeof window === "undefined") return;
    liveRef.current = false;
    bufferRef.current = [];

    // Step 1: open the socket first.
    let socket: WebSocket;
    try {
      socket = new WebSocket(roomSocketUrl(projectId));
    } catch {
      setStatus("offline");
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => {
      attemptsRef.current = 0;
      // Step 3: snapshot after the socket is open.
      void syncSnapshot();
    };

    socket.onmessage = (event) => {
      let envelope: WsEnvelope;
      try {
        envelope = JSON.parse(event.data as string) as WsEnvelope;
      } catch {
        return;
      }
      // Step 2: buffer until the snapshot has been reconciled.
      if (!liveRef.current) {
        bufferRef.current.push(envelope);
        return;
      }
      commit((s) => applyEvent(s, envelope));
    };

    socket.onclose = (event) => {
      liveRef.current = false;
      socketRef.current = null;
      if (disposedRef.current) return;

      if (event.code === 1008) {
        setStatus("denied");
        setError(
          "You're not a member of this room, or it doesn't exist. Join it from the dashboard.",
        );
        return;
      }
      if (event.code === 1013) {
        setStatus("resyncing");
        void syncSnapshot().then(() => connect());
        return;
      }
      setStatus("reconnecting");
      const attempt = attemptsRef.current++;
      const delay = Math.min(30000, 800 * 2 ** attempt) + Math.random() * 400;
      timerRef.current = setTimeout(connect, delay);
    };

    socket.onerror = () => {
      /* close handler drives recovery */
    };
  }, [projectId, syncSnapshot, commit]);

  useEffect(() => {
    disposedRef.current = false;
    setState(null);
    stateRef.current = null;
    setStatus("connecting");
    setError(null);
    connect();
    // Snapshot even if the socket never opens, so the room still renders.
    const fallback = setTimeout(() => {
      if (!stateRef.current) void syncSnapshot();
    }, 2500);
    return () => {
      disposedRef.current = true;
      clearTimeout(fallback);
      if (timerRef.current) clearTimeout(timerRef.current);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [connect, syncSnapshot]);

  return { state, status, error, refresh: syncSnapshot, commit };
}