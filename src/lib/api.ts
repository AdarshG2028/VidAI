import { getUserId } from "./identity";

export const API_BASE = (
  (import.meta.env["VITE_API_BASE_URL"] as string | undefined) ??
  "http://localhost:8000"
).replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, ...rest } = init;
  const headers = new Headers(rest.headers);
  headers.set("X-User-Id", getUserId());
  let body: BodyInit | null = (rest.body ?? null) as BodyInit | null;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...rest, headers, body });
  } catch {
    throw new ApiError(0, `Can't reach the VidAI backend at ${API_BASE}`);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const payload = (await res.json()) as { detail?: unknown };
      if (typeof payload?.detail === "string") detail = payload.detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/* ---------------- types ---------------- */

export type ApprovalPolicy = "admin" | "team";

export type Project = {
  id: string;
  owner_id: string;
  name: string | null;
  approval_policy: ApprovalPolicy;
  created_at: string;
};

export type Member = {
  user_id: string;
  role: string;
  joined_at: string;
};

export type VideoSummary = {
  id: string;
  original_filename?: string;
  name?: string;
  status?: string;
  created_at?: string;
};

export type Video = VideoSummary & {
  project_id?: string;
  analysis?: unknown;
};

export type ChatMessage = {
  id: string;
  role: string;
  sender_id: string | null;
  // For an assistant message this is a JSON string -- `{"type":"message",
  // "text":...}` or `{"type":"proposal",...}` -- not plain text. See
  // ChatPanel.tsx's parsePlannerContent, the one place this is parsed.
  content: string;
  created_at: string;
};

export type WorkflowStage = {
  stage?: string;
  name?: string;
  worker?: string;
  worker_name?: string;
  description?: string;
  params?: Record<string, unknown>;
  [k: string]: unknown;
};

export type Approval = {
  policy: ApprovalPolicy;
  approved_by: string[];
  rejected_by: string[];
  awaiting: string[];
  required: number;
};

export type Proposal = {
  id: string;
  project_id?: string;
  status?: string;
  summary?: string;
  reasoning?: string;
  discussion_summary?: string;
  workflow?: WorkflowStage[];
  created_at?: string;
  approval?: Approval;
  [k: string]: unknown;
};

export type Job = {
  id: string;
  status: string;
  workflow?: WorkflowStage[];
  current_stage?: number | string | null;
  total_stages?: number;
  attempts?: number;
  max_attempts?: number;
  created_at?: string;
  proposal_id?: string;
  [k: string]: unknown;
};

// The backend also sends a `download_url`, deliberately NOT typed here so
// nothing can reach for it: it is *relative* (`/artifacts?uri=...`) and
// carries no user_id, so in the browser it resolves against the app's own
// origin (:8080) instead of the API's (:8000) and 404s as an HTML page --
// which renders as a silently blank <video>. Always build the real URL
// with artifactUrl(), which is absolute and carries identity.
export type Artifact = {
  kind: string;
  uri: string;
};

export type ArtifactStage = {
  stage: string | number;
  worker_name?: string;
  artifacts: Artifact[];
};

// Matches backend/api/schemas/room.py's ExportResponse exactly -- an
// export has no uri/download_url of its own, only a list of per-kind
// artifacts (the final stage's assets: typically a "video", sometimes
// also an "srt" if the workflow burned/produced subtitles).
export type ExportItem = {
  job_id: string;
  workflow: string[];
  completed_at: string | null;
  artifacts: Artifact[];
};

export type Snapshot = {
  seq: number;
  project: Project;
  members: Member[];
  videos: VideoSummary[];
  messages: ChatMessage[];
  active_jobs: Job[];
  ended_jobs: Job[];
  exports: ExportItem[];
  proposals?: Proposal[];
};

export type PlannerResponse =
  | { type: "message"; text: string }
  | { type: "proposal"; summary: string; workflow: WorkflowStage[]; [k: string]: unknown };

/* ---------------- endpoints ---------------- */

export const api = {
  createProject: (name?: string) =>
    request<Project>("/projects", { method: "POST", json: { name } }),

  updateProject: (id: string, approval_policy: ApprovalPolicy) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", json: { approval_policy } }),

  getSnapshot: (id: string) => request<Snapshot>(`/projects/${id}`),

  sendMessage: (id: string, content: string) =>
    request<{ message_id: string; response: PlannerResponse }>(
      `/projects/${id}/messages`,
      { method: "POST", json: { content } },
    ),

  getMessages: (id: string) =>
    request<{ messages: ChatMessage[] }>(`/projects/${id}/messages`),

  getVideos: (id: string) => request<{ videos: VideoSummary[] }>(`/projects/${id}/videos`),

  uploadVideo: (id: string, file: File, name?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);
    return request<{
      video_id: string;
      project_id: string;
      job_id: string;
      name: string;
      status: string;
    }>(`/projects/${id}/videos`, { method: "POST", body: form });
  },

  renameVideo: (id: string, videoId: string, name: string) =>
    request<VideoSummary>(`/projects/${id}/videos/${videoId}`, {
      method: "PATCH",
      json: { name },
    }),

  previewProposal: (id: string, proposal_id?: string) =>
    request<{ job_id?: string }>(`/projects/${id}/preview-proposal`, {
      method: "POST",
      json: proposal_id ? { proposal_id } : {},
    }),

  inviteMember: (id: string, user_id: string) =>
    request<Member>(`/projects/${id}/members`, { method: "POST", json: { user_id } }),

  joinProject: (id: string) =>
    request<{ [k: string]: unknown }>(`/projects/${id}/join`, { method: "POST" }),

  getMembers: (id: string) => request<{ members: Member[] }>(`/projects/${id}/members`),

  getProposals: (id: string) =>
    request<{ proposals: Proposal[] }>(`/projects/${id}/proposals`),

  getProposal: (proposalId: string) => request<Proposal>(`/proposals/${proposalId}`),

  approveProposal: (proposalId: string) =>
    request<{ proposal: Proposal; job_id?: string }>(`/proposals/${proposalId}/approve`, {
      method: "POST",
    }),

  rejectProposal: (proposalId: string) =>
    request<{ proposal: Proposal; job_id?: string }>(`/proposals/${proposalId}/reject`, {
      method: "POST",
    }),

  getJob: (jobId: string) => request<Job>(`/jobs/${jobId}`),

  getJobArtifacts: (jobId: string) =>
    request<{ job_id: string; status: string; stages: ArtifactStage[] }>(
      `/jobs/${jobId}/artifacts`,
    ),

  cancelJob: (jobId: string) => request<Job>(`/jobs/${jobId}/cancel`, { method: "POST" }),
};

/** Playable/downloadable artifact URL — headers aren't possible here, so user_id is a query param. */
export function artifactUrl(uri: string, jobId?: string) {
  const params = new URLSearchParams({ uri });
  if (jobId) params.set("job_id", jobId);
  params.set("user_id", getUserId());
  return `${API_BASE}/artifacts?${params.toString()}`;
}

/** Playable/downloadable URL for a video's original upload, before any
 * edit has run on it — a separate route from artifactUrl since a fresh
 * upload's video_analysis job produces no downloadable asset. */
export function videoDownloadUrl(projectId: string, videoId: string) {
  const params = new URLSearchParams({ user_id: getUserId() });
  return `${API_BASE}/projects/${projectId}/videos/${videoId}/download?${params.toString()}`;
}

/** Actually saves a file, unlike `<a href=... download>` against a
 * cross-origin URL: the API and the app run on different origins/ports
 * (localhost:8080 vs :8000 in dev, and likely different domains in any
 * real deploy), and browsers silently ignore the `download` attribute on
 * a cross-origin link -- clicking it just navigates the tab away instead
 * of saving anything. Fetching the bytes ourselves and downloading from a
 * same-origin blob: URL is what makes "download" actually mean download. */
export async function downloadFile(url: string, filename: string) {
  const res = await fetch(url, { headers: { "X-User-Id": getUserId() } });
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}

export function roomSocketUrl(projectId: string) {
  const base = API_BASE.replace(/^http/, "ws");
  return `${base}/projects/${projectId}/ws?user_id=${encodeURIComponent(getUserId())}`;
}