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
  let body = rest.body;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...rest, headers, body });
  } catch {
    throw new ApiError(0, `Can't reach the VedAI backend at ${API_BASE}`);
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
  content: string;
  created_at: string;
  proposal_id?: string | null;
  metadata?: Record<string, unknown> | null;
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

export type Artifact = {
  kind: string;
  uri: string;
  download_url?: string;
};

export type ArtifactStage = {
  stage: string | number;
  worker_name?: string;
  artifacts: Artifact[];
};

export type ExportItem = {
  id?: string;
  job_id?: string;
  uri?: string;
  download_url?: string;
  created_at?: string;
  kind?: string;
  [k: string]: unknown;
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

export function roomSocketUrl(projectId: string) {
  const base = API_BASE.replace(/^http/, "ws");
  return `${base}/projects/${projectId}/ws?user_id=${encodeURIComponent(getUserId())}`;
}