import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Loader2, MessagesSquare } from "lucide-react";
import type { ChatMessage, Proposal } from "@/lib/api";
import { memberIdentity } from "@/lib/member-identity";
import { BrandMark } from "@/components/BrandMark";
import { EmptyState } from "./primitives";
import { ProposalCard } from "./ProposalCard";

const PROMPTS = [
  "Trim the first 10 seconds and normalize the audio",
  "Cut out the section between 1:20 and 1:45, then rejoin",
  "Transcribe this and burn in subtitles",
  "Find the filler words and tell me where they are",
];

function proposalIdFor(m: ChatMessage): string | null {
  if (m.proposal_id) return m.proposal_id;
  const meta = m.metadata as Record<string, unknown> | null | undefined;
  const id = meta?.["proposal_id"];
  return typeof id === "string" ? id : null;
}

function timeOf(iso?: string) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function ChatPanel({
  messages,
  proposals,
  userId,
  onSend,
  sending,
}: {
  messages: ChatMessage[];
  proposals: Proposal[];
  userId: string;
  onSend: (content: string) => Promise<void>;
  sending: boolean;
}) {
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const byId = useMemo(() => {
    const map = new Map<string, Proposal>();
    for (const p of proposals) if (p.id) map.set(p.id, p);
    return map;
  }, [proposals]);

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages.length, sending]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function submit() {
    const content = draft.trim();
    if (!content || sending) return;
    setDraft("");
    await onSend(content);
    inputRef.current?.focus();
  }

  return (
    <section className="panel relative flex min-h-0 flex-col overflow-hidden">
      <header className="flex items-center justify-between border-b border-border/70 px-5 py-4">
        <div>
          <h2 className="font-display text-[0.95rem]">Conversation</h2>
          <p className="text-xs text-muted-foreground">
            Describe the edit — the planner turns it into a workflow.
          </p>
        </div>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-6">
        {messages.length === 0 ? (
          <EmptyState
            icon={<MessagesSquare className="size-7" />}
            title="Nothing said yet"
            description="Upload a video, then tell the planner what you want. It answers with a plan your room can approve."
            action={
              <div className="flex flex-wrap justify-center gap-2">
                {PROMPTS.map((p) => (
                  <button
                    key={p}
                    onClick={() => {
                      setDraft(p);
                      inputRef.current?.focus();
                    }}
                    className="rounded-full border border-border bg-surface-2/60 px-3.5 py-1.5 text-xs text-muted-foreground transition-colors duration-200 hover:border-primary/40 hover:text-foreground"
                  >
                    {p}
                  </button>
                ))}
              </div>
            }
          />
        ) : (
          messages.map((m) => {
            const isAi = m.role !== "user" || !m.sender_id;
            const isMine = !isAi && m.sender_id === userId;
            const pid = proposalIdFor(m);
            const proposal = pid ? byId.get(pid) : undefined;

            if (proposal) {
              return (
                <div key={m.id} className="max-w-2xl">
                  <MessageMeta label="VedAI planner" ai time={timeOf(m.created_at)} />
                  <ProposalCard proposal={proposal} userId={userId} compact />
                </div>
              );
            }

            if (isAi) {
              return (
                <div key={m.id} className="animate-rise flex max-w-2xl gap-3">
                  <BrandMark className="mt-6 size-7 rounded-lg" />
                  <div className="min-w-0 flex-1">
                    <MessageMeta label="VedAI planner" ai time={timeOf(m.created_at)} />
                    <div className="rounded-2xl rounded-tl-md border border-ai/25 bg-ai/8 px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap">
                      {m.content}
                    </div>
                  </div>
                </div>
              );
            }

            if (isMine) {
              return (
                <div key={m.id} className="animate-rise flex justify-end">
                  <div className="max-w-xl">
                    <MessageMeta label="You" time={timeOf(m.created_at)} align="right" />
                    <div className="bg-gradient-accent rounded-2xl rounded-tr-md px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap text-primary-foreground">
                      {m.content}
                    </div>
                  </div>
                </div>
              );
            }

            const who = memberIdentity(m.sender_id ?? "");
            return (
              <div key={m.id} className="animate-rise flex max-w-2xl gap-3">
                <span
                  className="mt-6 grid size-7 shrink-0 place-items-center rounded-lg border text-[10px] font-semibold"
                  style={{
                    background: who.soft,
                    color: who.color,
                    borderColor: who.border,
                  }}
                >
                  {who.initials}
                </span>
                <div className="min-w-0 flex-1">
                  <MessageMeta
                    label={who.label}
                    time={timeOf(m.created_at)}
                    color={who.color}
                  />
                  <div
                    className="rounded-2xl rounded-tl-md border px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap"
                    style={{ background: who.soft, borderColor: who.border }}
                  >
                    {m.content}
                  </div>
                </div>
              </div>
            );
          })
        )}

        {sending ? (
          <div className="animate-rise flex items-center gap-3 text-sm text-ai">
            <BrandMark className="size-7 rounded-lg" />
            <span className="flex items-center gap-1.5">
              Planning
              <span className="flex gap-1">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="size-1.5 animate-bounce rounded-full bg-ai"
                    style={{ animationDelay: `${i * 120}ms` }}
                  />
                ))}
              </span>
            </span>
          </div>
        ) : null}
      </div>

      <div className="p-4">
        <div className="glass focus-within:border-primary/45 flex items-end gap-2 rounded-2xl p-2 transition-colors duration-200">
          <textarea
            ref={inputRef}
            value={draft}
            rows={1}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            placeholder="Describe the edit you want…"
            className="max-h-40 min-h-11 flex-1 resize-none bg-transparent px-3 py-2.5 text-sm outline-none placeholder:text-muted-foreground"
          />
          <button
            onClick={() => void submit()}
            disabled={sending || !draft.trim()}
            aria-label="Send message"
            className="bg-gradient-accent grid size-10 shrink-0 place-items-center rounded-xl text-primary-foreground transition-transform duration-200 ease-out hover:scale-105 disabled:opacity-40"
          >
            {sending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <ArrowUp className="size-4" />
            )}
          </button>
        </div>
      </div>
    </section>
  );
}

function MessageMeta({
  label,
  time,
  ai,
  align,
  color,
}: {
  label: string;
  time: string;
  ai?: boolean;
  align?: "right";
  color?: string;
}) {
  return (
    <p
      className={`mb-1.5 flex items-center gap-2 text-[11px] tracking-wide ${align === "right" ? "justify-end" : ""}`}
      style={{ color: color ?? undefined }}
    >
      <span className={ai ? "text-ai" : color ? "" : "text-muted-foreground"}>
        {label}
      </span>
      <span className="text-muted-foreground/70">{time}</span>
    </p>
  );
}