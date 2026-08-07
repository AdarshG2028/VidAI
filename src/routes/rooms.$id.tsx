import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Check, Copy, Loader2, ShieldCheck, TriangleAlert } from "lucide-react";
import { toast } from "sonner";
import { BrandMark, Wordmark } from "@/components/BrandMark";
import { ChatPanel } from "@/components/room/ChatPanel";
import { ProposalCard } from "@/components/room/ProposalCard";
import {
  ExportsPanel,
  JobsPanel,
  MembersPanel,
  VideosPanel,
} from "@/components/room/SidePanels";
import { EmptyState, Panel, Pill, Skeleton } from "@/components/room/primitives";
import { api, type ApprovalPolicy } from "@/lib/api";
import { getUserId } from "@/lib/identity";
import { memberIdentity, shortId } from "@/lib/member-identity";
import { rememberRoom } from "@/lib/rooms-store";
import { useRoom } from "@/hooks/useRoom";

export const Route = createFileRoute("/rooms/$id")({
  head: () => ({
    meta: [
      { title: "Room workspace — VidAI" },
      {
        name: "description",
        content:
          "Chat with the VidAI planner, review proposed edit workflows, approve them with your room, and watch renders finish live.",
      },
      { property: "og:title", content: "Room workspace — VidAI" },
      {
        property: "og:description",
        content: "Collaborative, chat-driven video editing in a shared VidAI room.",
      },
    ],
  }),
  component: RoomPage,
});

type Tab = "proposals" | "jobs" | "exports";

function RoomPage() {
  const { id } = Route.useParams();
  const [userId, setUserId] = useState("");
  const [tab, setTab] = useState<Tab>("proposals");
  const [sending, setSending] = useState(false);
  const [policyBusy, setPolicyBusy] = useState(false);
  const { state, status, error, refresh } = useRoom(id);

  useEffect(() => {
    setUserId(getUserId());
    rememberRoom(id);
  }, [id]);

  const proposals = useMemo(
    () =>
      [...(state?.proposals ?? [])].sort((a, b) =>
        String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")),
      ),
    [state?.proposals],
  );
  const pendingCount = proposals.filter((p) =>
    ["pending", "proposed", "open"].includes(String(p.status ?? "pending").toLowerCase()),
  ).length;

  async function send(content: string) {
    setSending(true);
    try {
      await api.sendMessage(id, content);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Message failed to send");
    } finally {
      setSending(false);
    }
  }

  async function setPolicy(policy: ApprovalPolicy) {
    setPolicyBusy(true);
    try {
      await api.updateProject(id, policy);
      await refresh();
      toast.success(`Approval policy set to ${policy}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not change the policy");
    } finally {
      setPolicyBusy(false);
    }
  }

  const isOwner = state?.project?.owner_id === userId;
  const me = memberIdentity(userId);

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="z-30 flex h-16 shrink-0 items-center gap-4 border-b border-border/60 px-5 backdrop-blur-xl">
        <Link
          to="/rooms"
          className="rounded-lg border border-border p-2 text-muted-foreground transition-colors duration-200 hover:border-primary/40 hover:text-foreground"
          aria-label="Back to rooms"
        >
          <ArrowLeft className="size-4" />
        </Link>
        <div className="flex items-center gap-2.5">
          <BrandMark className="size-8" />
          <Wordmark className="hidden text-base sm:block" />
        </div>
        <div className="mx-2 hidden h-6 w-px bg-border sm:block" />
        <div className="min-w-0">
          <h1 className="truncate text-sm leading-tight font-medium">
            {state?.project?.name || "Untitled room"}
          </h1>
          <button
            onClick={() => {
              void navigator.clipboard.writeText(id);
              toast.success("Room ID copied");
            }}
            className="inline-flex items-center gap-1.5 font-mono text-[10px] text-muted-foreground transition-colors hover:text-foreground"
          >
            {shortId(id)} <Copy className="size-3" />
          </button>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <ConnectionPill status={status} />
          {state ? (
            <div className="hidden items-center gap-1 rounded-full border border-border bg-surface/70 p-1 md:flex">
              {(["admin", "team"] as ApprovalPolicy[]).map((p) => {
                const active = state.project.approval_policy === p;
                return (
                  <button
                    key={p}
                    disabled={!isOwner || policyBusy}
                    onClick={() => void setPolicy(p)}
                    title={
                      isOwner
                        ? p === "admin"
                          ? "One approver decides"
                          : "Every active member must approve"
                        : "Only the owner can change the approval policy"
                    }
                    className={`rounded-full px-3 py-1 text-xs transition-colors duration-200 ${
                      active
                        ? "bg-primary/18 text-primary"
                        : "text-muted-foreground hover:text-foreground"
                    } ${!isOwner ? "cursor-not-allowed" : ""}`}
                  >
                    {policyBusy && !active ? (
                      <Loader2 className="size-3 animate-spin" />
                    ) : (
                      p
                    )}
                  </button>
                );
              })}
            </div>
          ) : null}
          <button
            onClick={() => {
              void navigator.clipboard.writeText(userId);
              toast.success("Your user ID is copied — send it to whoever is inviting you");
            }}
            className="grid size-8 place-items-center rounded-full border text-[11px] font-semibold transition-transform duration-200 hover:scale-110"
            style={{ background: me.soft, color: me.color, borderColor: me.border }}
            title={`${me.label} · click to copy your user ID`}
            aria-label="Copy your user ID"
          >
            {me.initials}
          </button>
        </div>
      </header>

      {status === "denied" ? (
        <div className="grid flex-1 place-items-center px-6">
          <div className="panel max-w-md p-8 text-center">
            <TriangleAlert className="mx-auto size-8 text-destructive" />
            <h2 className="mt-4 text-lg">You're not in this room</h2>
            <p className="mt-2 text-sm text-muted-foreground">{error}</p>
            <div className="mt-6 flex justify-center gap-3">
              <button
                onClick={async () => {
                  try {
                    await api.joinProject(id);
                    toast.success("Joined — reloading");
                    window.location.reload();
                  } catch (err) {
                    toast.error(err instanceof Error ? err.message : "Join failed");
                  }
                }}
                className="bg-gradient-accent rounded-xl px-4 py-2 text-sm font-medium text-primary-foreground"
              >
                Try joining
              </button>
              <Link
                to="/rooms"
                className="rounded-xl border border-border px-4 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                Back to rooms
              </Link>
            </div>
          </div>
        </div>
      ) : !state ? (
        <LoadingRoom error={error} onRetry={() => void refresh()} />
      ) : (
        <main className="grid min-h-0 flex-1 gap-4 overflow-hidden p-4 lg:grid-cols-[19rem_minmax(0,1fr)_23rem]">
          <div className="hidden h-full min-h-0 flex-col gap-4 lg:flex">
            <MembersPanel
              members={state.members}
              project={state.project}
              userId={userId}
              onChanged={() => void refresh()}
            />
            <VideosPanel
              videos={state.videos}
              projectId={id}
              onChanged={() => void refresh()}
              className="flex-1"
            />
          </div>

          <ChatPanel
            messages={state.messages}
            proposals={state.proposals}
            userId={userId}
            onSend={send}
            sending={sending}
          />

          <div className="flex min-h-0 flex-col gap-3">
            <div className="flex gap-1 rounded-xl border border-border bg-surface/60 p-1">
              {(
                [
                  ["proposals", `Proposals${pendingCount ? ` · ${pendingCount}` : ""}`],
                  ["jobs", `Jobs${state.active_jobs.length ? ` · ${state.active_jobs.length}` : ""}`],
                  ["exports", `Exports${state.exports.length ? ` · ${state.exports.length}` : ""}`],
                ] as [Tab, string][]
              ).map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => setTab(key)}
                  className={`flex-1 rounded-lg px-3 py-1.5 text-xs transition-colors duration-200 ${
                    tab === key
                      ? "bg-primary/18 text-primary"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* h-full on each panel, not just min-h-0 here: Panel is a
                block-level <section>, so without an explicit height it
                sizes to its content, overflows this box and gets clipped
                by an ancestor's overflow-hidden -- its own
                overflow-y-auto body never gets the chance to scroll. */}
            <div className="min-h-0 flex-1">
              {tab === "proposals" ? (
                <Panel
                  title="Proposals"
                  subtitle={
                    state.project.approval_policy === "team"
                      ? "Every active member must approve; one reject ends it"
                      : "A single approver decides"
                  }
                  className="h-full"
                  bodyClassName="p-4 space-y-4"
                >
                  {proposals.length === 0 ? (
                    <EmptyState
                      icon={<ShieldCheck className="size-6" />}
                      title="No plans on the table"
                      description="Ask the planner for an edit. Anything it proposes shows up here for the room to vote on."
                    />
                  ) : (
                    proposals.map((p) => (
                      <ProposalCard key={p.id} proposal={p} userId={userId} compact />
                    ))
                  )}
                </Panel>
              ) : tab === "jobs" ? (
                <JobsPanel
                  active={state.active_jobs}
                  ended={state.ended_jobs}
                  onChanged={() => void refresh()}
                  className="h-full"
                />
              ) : (
                <ExportsPanel exports={state.exports} className="h-full" />
              )}
            </div>
          </div>
        </main>
      )}

      {state ? (
        <div className="flex shrink-0 gap-4 overflow-x-auto border-t border-border/60 p-4 lg:hidden">
          <div className="min-w-[18rem] flex-1">
            <MembersPanel
              members={state.members}
              project={state.project}
              userId={userId}
              onChanged={() => void refresh()}
            />
          </div>
          <div className="min-w-[18rem] flex-1">
            <VideosPanel
              videos={state.videos}
              projectId={id}
              onChanged={() => void refresh()}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ConnectionPill({ status }: { status: string }) {
  const map: Record<string, { tone: string; label: string; pulse?: boolean }> = {
    connecting: { tone: "accent", label: "Connecting", pulse: true },
    live: { tone: "success", label: "Live", pulse: true },
    resyncing: { tone: "warning", label: "Resyncing", pulse: true },
    reconnecting: { tone: "warning", label: "Reconnecting", pulse: true },
    denied: { tone: "danger", label: "No access" },
    offline: { tone: "danger", label: "Offline" },
  };
  const s = map[status] ?? map["connecting"]!;
  return (
    <Pill tone={s.tone} pulse={s.pulse ?? false}>
      {s.label}
    </Pill>
  );
}

function LoadingRoom({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  if (error) {
    return (
      <div className="grid flex-1 place-items-center px-6">
        <div className="panel max-w-md p-8 text-center">
          <TriangleAlert className="mx-auto size-8 text-warning" />
          <h2 className="mt-4 text-lg">Couldn't load this room</h2>
          <p className="mt-2 text-sm text-muted-foreground">{error}</p>
          <button
            onClick={onRetry}
            className="bg-gradient-accent mt-6 inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            <Check className="size-4" /> Try again
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="grid min-h-0 flex-1 gap-4 p-4 lg:grid-cols-[19rem_minmax(0,1fr)_23rem]">
      <div className="hidden flex-col gap-4 lg:flex">
        <Skeleton className="h-64 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
      <Skeleton className="h-full w-full" />
      <Skeleton className="hidden h-full w-full lg:block" />
    </div>
  );
}