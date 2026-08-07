import { useRef, useState } from "react";
import {
  Ban,
  Check,
  Copy,
  Download,
  FileVideo,
  Loader2,
  Play,
  Sparkles,
  Upload,
  UserPlus,
  Users,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  artifactUrl,
  downloadFile,
  videoDownloadUrl,
  type ExportItem,
  type Job,
  type Member,
  type Project,
  type VideoSummary,
} from "@/lib/api";
import { memberIdentity, shortId } from "@/lib/member-identity";
import { cn } from "@/lib/utils";
import { EmptyState, Panel, Pill, statusTone } from "./primitives";

export function MembersPanel({
  members,
  project,
  userId,
  onChanged,
}: {
  members: Member[];
  project: Project;
  userId: string;
  onChanged: () => void;
}) {
  const [invite, setInvite] = useState("");
  const [busy, setBusy] = useState(false);
  const isOwner = project.owner_id === userId;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!invite.trim()) return;
    setBusy(true);
    try {
      await api.inviteMember(project.id, invite.trim());
      setInvite("");
      toast.success("Member invited");
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Invite failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Members"
      subtitle={`${members.length} in this room · ${project.approval_policy === "team" ? "everyone approves" : "one approver decides"}`}
      bodyClassName="p-4 space-y-2"
    >
      <IdentityCard userId={userId} />

      {members.length === 0 ? (
        <EmptyState
          icon={<Users className="size-6" />}
          title="Just you so far"
          description="Invite collaborators by their user ID to review and approve edits together."
        />
      ) : (
        members.map((m) => {
          const who = memberIdentity(m.user_id);
          return (
            <div
              key={m.user_id}
              className="animate-rise flex items-center gap-3 rounded-xl border border-border/70 bg-background/40 px-3 py-2.5"
            >
              <span
                className="grid size-8 shrink-0 place-items-center rounded-lg border text-[11px] font-semibold"
                style={{ background: who.soft, color: who.color, borderColor: who.border }}
              >
                {who.initials}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">
                  {who.label}
                  {m.user_id === userId ? (
                    <span className="text-muted-foreground"> · you</span>
                  ) : null}
                </p>
                <p className="truncate font-mono text-[10px] text-muted-foreground">
                  {shortId(m.user_id)}
                </p>
              </div>
              <Pill tone={m.role === "owner" ? "accent" : "neutral"}>{m.role}</Pill>
              <button
                disabled
                title="Removing members isn't available yet"
                className="cursor-not-allowed rounded-lg border border-border p-1.5 text-muted-foreground opacity-40"
                aria-label="Remove member (unavailable)"
              >
                <Ban className="size-3.5" />
              </button>
            </div>
          );
        })
      )}

      {isOwner ? (
        <div className="space-y-1.5 pt-3">
          <p className="text-[11px] text-muted-foreground">
            Invite someone — paste the ID they copied from their own VidAI.
          </p>
          <form onSubmit={submit} className="flex gap-2">
            <input
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
              placeholder="Paste their user ID…"
              className="min-w-0 flex-1 rounded-xl border border-input bg-background/60 px-3 py-2 font-mono text-[11px] outline-none transition-colors focus:border-primary/50"
            />
            <button
              disabled={busy || !invite.trim()}
              className="bg-gradient-accent grid size-9 shrink-0 place-items-center rounded-xl text-primary-foreground transition-transform duration-200 hover:scale-105 disabled:opacity-40"
              aria-label="Invite member"
            >
              {busy ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <UserPlus className="size-4" />
              )}
            </button>
          </form>
        </div>
      ) : (
        <p className="pt-2 text-xs text-muted-foreground">
          Only the room owner can invite members.
        </p>
      )}
    </Panel>
  );
}

/** Your own user ID, front and centre and one click to copy.
 *
 * There is no account system (identity is a localStorage UUID, see
 * lib/identity.ts), so being invited means telling someone this string --
 * it was previously only readable as a hover tooltip on the avatar, which
 * you cannot select or copy. */
function IdentityCard({ userId }: { userId: string }) {
  const [copied, setCopied] = useState(false);
  const me = memberIdentity(userId);

  function copy() {
    void navigator.clipboard.writeText(userId);
    setCopied(true);
    toast.success("Your user ID is copied — send it to whoever is inviting you");
    setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="rounded-xl border border-primary/25 bg-primary/[0.06] p-3">
      <div className="flex items-center gap-2">
        <span
          className="grid size-7 shrink-0 place-items-center rounded-lg border text-[10px] font-semibold"
          style={{ background: me.soft, color: me.color, borderColor: me.border }}
        >
          {me.initials}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-xs leading-tight font-medium">Your user ID</p>
          <p className="text-[10px] leading-tight text-muted-foreground">
            Share this to get invited
          </p>
        </div>
        <button
          onClick={copy}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-primary/30 bg-primary/10 px-2.5 py-1.5 text-[11px] text-primary transition-colors duration-200 hover:bg-primary/20"
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <button
        onClick={copy}
        title="Click to copy"
        className="mt-2 w-full rounded-lg border border-border/70 bg-background/60 px-2.5 py-1.5 text-left font-mono text-[10px] leading-relaxed break-all text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
      >
        {userId || "…"}
      </button>
    </div>
  );
}

export function VideosPanel({
  videos,
  projectId,
  onChanged,
  className,
}: {
  videos: VideoSummary[];
  projectId: string;
  onChanged: () => void;
  className?: string;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  // The original upload, before any edit — not an artifact of any job, so
  // it can't go through the /artifacts route. Only one open at a time.
  const [previewing, setPreviewing] = useState<string | null>(null);

  async function upload(file: File) {
    setUploading(true);
    try {
      await api.uploadVideo(projectId, file);
      toast.success(`${file.name} uploaded — analysis started`);
      onChanged();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Upload failed";
      toast.error(
        msg.includes("409") ? "A video with that name already exists in this room" : msg,
      );
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <Panel
      title="Video library"
      subtitle="Source footage available to the planner"
      className={cn("min-h-0", className)}
      bodyClassName="p-4 space-y-2"
      action={
        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-primary/30 bg-primary/10 px-2.5 py-1.5 text-xs text-primary transition-colors duration-200 hover:bg-primary/18 disabled:opacity-50"
        >
          {uploading ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Upload className="size-3.5" />
          )}
          Upload
        </button>
      }
    >
      <input
        ref={fileRef}
        type="file"
        accept="video/*"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void upload(f);
        }}
      />
      {videos.length === 0 ? (
        <EmptyState
          icon={<FileVideo className="size-6" />}
          title="No footage yet"
          description="Upload a clip to give the planner something to work with. Analysis runs automatically."
          action={
            <button
              onClick={() => fileRef.current?.click()}
              className="bg-gradient-accent rounded-full px-4 py-2 text-xs font-medium text-primary-foreground transition-transform duration-200 hover:scale-[1.03]"
            >
              Choose a video
            </button>
          }
        />
      ) : (
        videos.map((v) => {
          const open = previewing === v.id;
          const href = videoDownloadUrl(projectId, v.id);
          return (
            <div
              key={v.id}
              className="animate-rise overflow-hidden rounded-xl border border-border/70 bg-background/40"
            >
              <div className="flex items-center gap-3 px-3 py-2.5">
                <FileVideo className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">{v.name || v.original_filename || v.id}</p>
                  <p className="truncate font-mono text-[10px] text-muted-foreground">
                    {v.original_filename ?? shortId(v.id)}
                  </p>
                </div>
                <Pill tone={statusTone(v.status)} pulse={v.status === "analyzing"}>
                  {v.status ?? "ready"}
                </Pill>
                <button
                  onClick={() => setPreviewing(open ? null : v.id)}
                  className="rounded-lg border border-border p-1.5 text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                  aria-label={open ? "Hide preview" : "Preview uploaded video"}
                  title="Preview the original upload"
                >
                  <Play className="size-3.5" />
                </button>
                <button
                  onClick={() => {
                    downloadFile(href, v.original_filename ?? v.name ?? "video.mp4").catch((err: unknown) => {
                      toast.error(err instanceof Error ? err.message : "Download failed");
                    });
                  }}
                  className="rounded-lg border border-border p-1.5 text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                  aria-label="Download original upload"
                  title="Download the original upload"
                >
                  <Download className="size-3.5" />
                </button>
              </div>
              {open ? (
                <video
                  src={href}
                  controls
                  autoPlay
                  preload="metadata"
                  className="aspect-video w-full border-t border-border/70 bg-black/60"
                />
              ) : null}
            </div>
          );
        })
      )}
    </Panel>
  );
}

function stageCount(job: Job) {
  const total = job.total_stages ?? job.workflow?.length ?? 0;
  const current =
    typeof job.current_stage === "number"
      ? job.current_stage
      : Number(job.current_stage ?? 0) || 0;
  return { total, current };
}

export function JobsPanel({
  active,
  ended,
  onChanged,
  className,
}: {
  active: Job[];
  ended: Job[];
  onChanged: () => void;
  className?: string;
}) {
  return (
    <Panel
      title="Jobs"
      subtitle="Execution of approved workflows"
      className={cn(className)}
      bodyClassName="p-4 space-y-3"
    >
      {active.length === 0 && ended.length === 0 ? (
        <EmptyState
          icon={<Sparkles className="size-6" />}
          title="Nothing rendering"
          description="Once a proposal is approved, its stages execute here with live progress."
        />
      ) : null}

      {active.map((job) => {
        const { total, current } = stageCount(job);
        const pct = total ? Math.min(100, Math.round((current / total) * 100)) : 12;
        return (
          <div
            key={job.id}
            className="animate-rise rounded-xl border border-primary/25 bg-background/40 p-4"
          >
            <div className="flex items-center justify-between gap-3">
              <p className="font-mono text-xs text-muted-foreground">{shortId(job.id)}</p>
              <Pill tone="accent" pulse>
                {job.status}
              </Pill>
            </div>
            <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-2">
              <div
                className="bg-gradient-accent h-full rounded-full transition-[width] duration-500 ease-out"
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
              <span>
                Stage {current || 1} of {total || "?"}
                {job.attempts ? ` · attempt ${job.attempts}/${job.max_attempts ?? "?"}` : ""}
              </span>
              <button
                onClick={async () => {
                  try {
                    await api.cancelJob(job.id);
                    toast.success("Cancel requested");
                    onChanged();
                  } catch (err) {
                    toast.error(err instanceof Error ? err.message : "Cancel failed");
                  }
                }}
                className="transition-colors hover:text-destructive"
              >
                Cancel
              </button>
            </div>
          </div>
        );
      })}

      {ended.map((job) => (
        <div
          key={job.id}
          className="animate-rise flex items-center justify-between gap-3 rounded-xl border border-border/70 bg-background/40 px-4 py-3"
        >
          <p className="font-mono text-xs text-muted-foreground">{shortId(job.id)}</p>
          <Pill tone={statusTone(job.status)}>{job.status}</Pill>
        </div>
      ))}
    </Panel>
  );
}

export function ExportsPanel({
  exports,
  className,
}: {
  exports: ExportItem[];
  className?: string;
}) {
  // Newest first. The snapshot already arrives in that order (ordered by
  // completed_at, see ProjectJobRepository.list_completed_jobs), but a
  // live `export.completed` event is appended to the end of the list by
  // useRoom's upsert -- so without sorting here a render that finishes
  // while you watch lands at the bottom, then jumps to the top on the
  // next reload. Sorting in the component keeps both paths consistent.
  const ordered = [...exports].sort((a, b) =>
    String(b.completed_at ?? "").localeCompare(String(a.completed_at ?? "")),
  );

  return (
    <Panel
      title="Exports"
      subtitle="Finished renders · newest first"
      className={cn(className)}
      bodyClassName="p-4 space-y-4"
    >
      {ordered.length === 0 ? (
        <EmptyState
          icon={<Play className="size-6" />}
          title="No renders yet"
          description="Approved workflows that reach the render stage land here, playable and downloadable."
        />
      ) : (
        ordered.map((ex, i) => {
          // An export is a set of per-kind artifacts (typically "video",
          // sometimes also "srt" if the workflow burned/produced
          // subtitles) -- not a single uri of its own. Always via
          // artifactUrl: the backend's own download_url is relative and
          // identity-less, see the Artifact type in lib/api.ts.
          const video = ex.artifacts.find((a) => a.kind === "video");
          const src = video ? artifactUrl(video.uri, ex.job_id) : "";
          return (
            <div
              key={ex.job_id ?? i}
              className="animate-rise overflow-hidden rounded-xl border border-border/70 bg-background/40"
            >
              {src ? (
                <video
                  src={src}
                  controls
                  preload="metadata"
                  className="aspect-video w-full bg-black/60"
                />
              ) : null}
              <div className="flex items-center justify-between gap-3 px-4 py-3">
                <div className="min-w-0">
                  <p className="truncate text-sm">
                    {ex.workflow?.length ? ex.workflow.join(" → ") : "Final render"}
                  </p>
                  <p className="truncate font-mono text-[10px] text-muted-foreground">
                    {shortId(ex.job_id ?? "")}
                  </p>
                </div>
                <div className="flex items-center gap-1.5">
                  {src ? (
                    <button
                      onClick={() => {
                        downloadFile(src, `${ex.job_id}.mp4`).catch((err: unknown) => {
                          toast.error(err instanceof Error ? err.message : "Download failed");
                        });
                      }}
                      className="rounded-lg border border-border p-2 text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                      aria-label="Download export"
                    >
                      <Download className="size-3.5" />
                    </button>
                  ) : null}
                  {src ? (
                    <button
                      onClick={() => {
                        void navigator.clipboard.writeText(src);
                        toast.success("Link copied");
                      }}
                      className="rounded-lg border border-border p-2 text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                      aria-label="Copy link"
                    >
                      <Copy className="size-3.5" />
                    </button>
                  ) : null}
                </div>
              </div>
              {ex.artifacts.length > 1 ? (
                <div className="flex flex-wrap gap-1.5 border-t border-border/70 px-4 py-2.5">
                  {ex.artifacts.map((a) => (
                    <button
                      key={a.kind}
                      onClick={() => {
                        const url = artifactUrl(a.uri, ex.job_id);
                        downloadFile(url, `${ex.job_id}-${a.kind}`).catch((err: unknown) => {
                          toast.error(err instanceof Error ? err.message : "Download failed");
                        });
                      }}
                      className="rounded-full border border-border bg-surface-2/60 px-2.5 py-1 text-[10px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                    >
                      {a.kind}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })
      )}
    </Panel>
  );
}