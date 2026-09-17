import { useEffect, useState } from "react";
import { Check, ChevronDown, Loader2, ShieldCheck, X } from "lucide-react";
import { toast } from "sonner";
import { api, type Approval, type Proposal } from "@/lib/api";
import { memberIdentity } from "@/lib/member-identity";
import { Pill } from "./primitives";
import { cn } from "@/lib/utils";

function stageLabel(stage: unknown, i: number) {
  if (typeof stage === "string") return stage;
  const s = (stage ?? {}) as Record<string, unknown>;
  return (
    (s["stage"] as string) ||
    (s["name"] as string) ||
    (s["worker_name"] as string) ||
    (s["worker"] as string) ||
    `stage ${i + 1}`
  );
}

export function ProposalCard({
  proposal,
  userId,
  compact,
}: {
  proposal: Proposal;
  userId: string;
  compact?: boolean;
}) {
  const [approval, setApproval] = useState<Approval | undefined>(proposal.approval);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [open, setOpen] = useState(!compact);

  useEffect(() => {
    setApproval(proposal.approval);
    if (proposal.approval || !proposal.id) return;
    let cancelled = false;
    api
      .getProposal(proposal.id)
      .then((p) => !cancelled && setApproval(p.approval))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [proposal]);

  const status = String(proposal.status ?? "pending").toLowerCase();
  const decided = status !== "pending" && status !== "proposed" && status !== "open";
  const alreadyVoted =
    approval?.approved_by?.includes(userId) || approval?.rejected_by?.includes(userId);
  const stages = (proposal.workflow ?? []) as unknown[];

  async function vote(kind: "approve" | "reject") {
    setBusy(kind);
    try {
      const res =
        kind === "approve"
          ? await api.approveProposal(proposal.id)
          : await api.rejectProposal(proposal.id);
      setApproval(res.proposal?.approval ?? approval);
      toast.success(
        kind === "approve"
          ? res.job_id
            ? "Approved — execution started"
            : "Approval recorded"
          : "Proposal rejected",
      );
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Vote failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className="animate-rise relative overflow-hidden rounded-2xl border border-primary/25 bg-surface/80">
      <div className="bg-gradient-accent absolute inset-x-0 top-0 h-px opacity-80" />
      <header className="flex items-start justify-between gap-3 px-5 pt-5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-primary" />
            <span className="text-xs tracking-[0.16em] text-primary uppercase">
              Proposed workflow
            </span>
          </div>
          <h3 className="mt-2 text-base leading-snug">
            {proposal.summary || "Edit plan"}
          </h3>
        </div>
        <Pill
          tone={
            status === "approved"
              ? "success"
              : status === "rejected"
                ? "danger"
                : "accent"
          }
          pulse={!decided}
        >
          {status}
        </Pill>
      </header>

      <div className="px-5 pt-4">
        <ol className="space-y-1.5">
          {stages.length === 0 ? (
            <li className="text-sm text-muted-foreground">No stages listed.</li>
          ) : (
            stages.map((stage, i) => (
              <li
                key={i}
                className="animate-rise flex items-center gap-3 rounded-lg border border-border/70 bg-background/40 px-3 py-2"
                style={{ animationDelay: `${i * 45}ms` }}
              >
                <span className="grid size-5 shrink-0 place-items-center rounded-md bg-primary/15 font-mono text-[10px] text-primary">
                  {i + 1}
                </span>
                <span className="truncate font-mono text-xs">{stageLabel(stage, i)}</span>
              </li>
            ))
          )}
        </ol>
      </div>

      {(proposal.reasoning || proposal.discussion_summary) && (
        <div className="px-5 pt-4">
          <button
            onClick={() => setOpen((v) => !v)}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            <ChevronDown
              className={cn(
                "size-3.5 transition-transform duration-200",
                open && "rotate-180",
              )}
            />
            Reasoning & discussion
          </button>
          {open ? (
            <div className="animate-rise mt-3 space-y-3 rounded-xl border border-border/70 bg-background/40 p-4">
              {proposal.reasoning ? (
                <div>
                  <p className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    Why
                  </p>
                  <p className="mt-1 text-sm leading-relaxed">{proposal.reasoning}</p>
                </div>
              ) : null}
              {proposal.discussion_summary ? (
                <div>
                  <p className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    Discussion
                  </p>
                  <p className="mt-1 text-sm leading-relaxed">
                    {proposal.discussion_summary}
                  </p>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      )}

      {approval ? (
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border/70 px-5 py-3 text-xs text-muted-foreground">
          <span>
            {approval.policy === "team"
              ? "Every active member must approve"
              : "One approver decides"}
          </span>
          <span className="text-foreground">
            {approval.approved_by?.length ?? 0}/{approval.required ?? 1} approved
          </span>
          <div className="flex -space-x-1.5">
            {(approval.approved_by ?? []).map((id) => {
              const m = memberIdentity(id);
              return (
                <span
                  key={id}
                  title={`${m.label} approved`}
                  className="grid size-5 place-items-center rounded-full border text-[9px] font-semibold"
                  style={{ background: m.soft, color: m.color, borderColor: m.border }}
                >
                  {m.initials}
                </span>
              );
            })}
          </div>
          {(approval.rejected_by?.length ?? 0) > 0 ? (
            <span className="text-destructive">
              {approval.rejected_by.length} rejected
            </span>
          ) : null}
          {(approval.awaiting?.length ?? 0) > 0 ? (
            <span>waiting on {approval.awaiting.length}</span>
          ) : null}
        </div>
      ) : null}

      <footer className="flex items-center gap-2.5 px-5 pb-5">
        {decided ? (
          <p className="text-sm text-muted-foreground">
            This proposal is {status}. No further votes are needed.
          </p>
        ) : (
          <>
            <button
              onClick={() => void vote("approve")}
              disabled={!!busy || alreadyVoted}
              className="bg-gradient-accent inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-primary-foreground transition-transform duration-200 ease-out hover:scale-[1.02] disabled:opacity-45"
            >
              {busy === "approve" ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Check className="size-4" />
              )}
              Approve
            </button>
            <button
              onClick={() => void vote("reject")}
              disabled={!!busy || alreadyVoted}
              className="inline-flex items-center gap-2 rounded-xl border border-border px-4 py-2 text-sm transition-colors duration-200 hover:border-destructive/50 hover:text-destructive disabled:opacity-45"
            >
              {busy === "reject" ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <X className="size-4" />
              )}
              Reject
            </button>
            {alreadyVoted ? (
              <span className="text-xs text-muted-foreground">Your vote is in.</span>
            ) : null}
          </>
        )}
      </footer>
    </article>
  );
}