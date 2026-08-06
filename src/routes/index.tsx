import { createFileRoute, Link } from "@tanstack/react-router";
import {
  ArrowRight,
  AudioLines,
  Captions,
  Crop,
  Film,
  FlipHorizontal2,
  Layers,
  MessageSquareText,
  Maximize2,
  Palette,
  Rows3,
  Scissors,
  ScissorsLineDashed,
  RotateCcw,
  Sparkle,
  SquareStack,
  Users,
  Waves,
} from "lucide-react";
import { BrandMark, Wordmark } from "@/components/BrandMark";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "VedAI — Edit video by describing it" },
      {
        name: "description",
        content:
          "VedAI turns a conversation into a finished cut. Describe the edit, review the AI's plan, approve it with your team, and get the render.",
      },
      { property: "og:title", content: "VedAI — Edit video by describing it" },
      {
        property: "og:description",
        content:
          "A collaborative, chat-driven AI video editor. No timeline, no dragging — just describe the edit and approve the plan.",
      },
    ],
  }),
  component: Landing,
});

const CAPABILITIES = [
  { icon: Crop, name: "Crop", desc: "Reframe to any region of the source frame." },
  { icon: Maximize2, name: "Resize", desc: "Scale to a target resolution or aspect." },
  { icon: RotateCcw, name: "Rotate", desc: "Turn footage by any quarter or angle." },
  { icon: FlipHorizontal2, name: "Flip", desc: "Mirror horizontally or vertically." },
  { icon: SquareStack, name: "Pad", desc: "Letterbox or pillarbox to fit a frame." },
  { icon: Palette, name: "Color adjust", desc: "Brightness, contrast, saturation, gamma." },
  { icon: AudioLines, name: "Audio normalize", desc: "Even out loudness across the cut." },
  { icon: Waves, name: "Silence removal", desc: "Strip dead air from the audio track." },
  { icon: Scissors, name: "Trim", desc: "Keep a range, drop the rest." },
  {
    icon: ScissorsLineDashed,
    name: "Remove middle",
    desc: "Cut a segment out and rejoin the halves.",
  },
  { icon: Layers, name: "Merge clips", desc: "Join multiple uploads into one timeline." },
  { icon: Captions, name: "Transcribe + subtitles", desc: "Transcribe and burn captions in." },
  { icon: Rows3, name: "Scene detection", desc: "Find the natural cut points." },
  { icon: Sparkle, name: "Filler-word detection", desc: "Flag every um, uh and false start." },
  { icon: Film, name: "Final render", desc: "Encode and deliver the finished export." },
];

const STEPS = [
  {
    n: "01",
    title: "Open a room",
    body: "A room is a shared project. Upload your footage and invite whoever needs a say.",
  },
  {
    n: "02",
    title: "Describe the edit",
    body: "Type it the way you'd say it. The planner turns intent into an ordered workflow of real operations.",
  },
  {
    n: "03",
    title: "Approve together",
    body: "Every proposal shows its stages and reasoning. One approver, or the whole room — your policy.",
  },
  {
    n: "04",
    title: "Watch it render",
    body: "Approved plans execute stage by stage, live in the room. The export lands playable.",
  },
];

function Landing() {
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-40 border-b border-border/60 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-6">
          <div className="flex items-center gap-3">
            <BrandMark />
            <Wordmark />
          </div>
          <nav className="hidden items-center gap-8 text-sm text-muted-foreground md:flex">
            <a href="#capabilities" className="transition-colors hover:text-foreground">
              Capabilities
            </a>
            <a href="#how" className="transition-colors hover:text-foreground">
              How it works
            </a>
          </nav>
          <Link
            to="/rooms"
            className="bg-gradient-accent glow-accent inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-medium text-primary-foreground transition-transform duration-200 ease-out hover:scale-[1.03]"
          >
            Open rooms <ArrowRight className="size-4" />
          </Link>
        </div>
      </header>

      <main>
        <section className="grid-noise relative overflow-hidden border-b border-border/60">
          <div className="mx-auto max-w-6xl px-6 pt-24 pb-28">
            <div className="animate-rise max-w-3xl">
              <span className="inline-flex items-center gap-2 rounded-full border border-border bg-surface/70 px-3 py-1 text-xs tracking-wide text-muted-foreground uppercase">
                <span className="animate-pulse-ring size-1.5 rounded-full bg-primary" />
                Conversational editing, not timelines
              </span>
              <h1 className="mt-7 text-5xl leading-[1.03] font-semibold sm:text-6xl md:text-7xl">
                Edit video by
                <br />
                <span className="text-gradient">describing the edit.</span>
              </h1>
              <p className="mt-7 max-w-xl text-lg leading-relaxed text-muted-foreground">
                VedAI is a collaborative room where you talk to a planner instead of
                dragging clips. It proposes a structured workflow, your team approves it,
                and the render comes back finished.
              </p>
              <div className="mt-10 flex flex-wrap items-center gap-4">
                <Link
                  to="/rooms"
                  className="bg-gradient-accent glow-accent inline-flex items-center gap-2 rounded-full px-6 py-3 text-sm font-medium text-primary-foreground transition-transform duration-200 ease-out hover:scale-[1.03]"
                >
                  Start a room <ArrowRight className="size-4" />
                </Link>
                <a
                  href="#how"
                  className="rounded-full border border-border px-6 py-3 text-sm text-muted-foreground transition-colors duration-200 hover:border-primary/40 hover:text-foreground"
                >
                  See how it works
                </a>
              </div>
            </div>

            <div className="animate-rise glass mt-20 overflow-hidden rounded-3xl p-1.5 [animation-delay:120ms]">
              <div className="rounded-[1.35rem] border border-border/70 bg-background/60 p-6 sm:p-8">
                <div className="flex flex-col gap-5">
                  <ChatPreviewRow
                    align="right"
                    label="You"
                    text="Trim the first 12 seconds, cut the dead air, and burn in subtitles."
                  />
                  <ChatPreviewRow
                    align="left"
                    label="VedAI planner"
                    ai
                    text="Proposed workflow — trim · silence removal · transcribe · subtitles · render. Awaiting approval from 2 members."
                  />
                  <div className="flex flex-wrap gap-2 pt-1">
                    {["trim", "silence-removal", "transcribe", "subtitles", "render"].map(
                      (s, i) => (
                        <span
                          key={s}
                          className="animate-rise rounded-lg border border-primary/25 bg-primary/10 px-3 py-1 font-mono text-xs text-primary"
                          style={{ animationDelay: `${200 + i * 70}ms` }}
                        >
                          {i + 1}. {s}
                        </span>
                      ),
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section id="capabilities" className="border-b border-border/60">
          <div className="mx-auto max-w-6xl px-6 py-24">
            <div className="max-w-2xl">
              <p className="text-xs tracking-[0.2em] text-primary uppercase">
                What the AI can reach for
              </p>
              <h2 className="mt-4 text-3xl sm:text-4xl">
                Fifteen operations, composed by conversation.
              </h2>
              <p className="mt-4 text-muted-foreground">
                These aren't buttons. You describe the outcome, and the planner assembles
                the right stages in the right order.
              </p>
            </div>
            <div className="mt-12 grid gap-px overflow-hidden rounded-2xl border border-border bg-border sm:grid-cols-2 lg:grid-cols-3">
              {CAPABILITIES.map(({ icon: Icon, name, desc }) => (
                <div
                  key={name}
                  className="group bg-surface/80 p-6 transition-colors duration-200 hover:bg-surface-2/80"
                >
                  <Icon className="size-5 text-primary transition-transform duration-200 group-hover:scale-110" />
                  <h3 className="mt-4 text-base">{name}</h3>
                  <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
                    {desc}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="how" className="border-b border-border/60">
          <div className="mx-auto max-w-6xl px-6 py-24">
            <h2 className="max-w-xl text-3xl sm:text-4xl">
              A room, a conversation, a consensus, a cut.
            </h2>
            <div className="mt-14 grid gap-10 md:grid-cols-2 lg:grid-cols-4">
              {STEPS.map((s) => (
                <div key={s.n} className="border-t border-border pt-6">
                  <span className="font-mono text-xs text-primary">{s.n}</span>
                  <h3 className="mt-3 text-lg">{s.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                    {s.body}
                  </p>
                </div>
              ))}
            </div>
            <div className="mt-16 grid gap-6 sm:grid-cols-3">
              <Highlight
                icon={<MessageSquareText className="size-5" />}
                title="No timeline"
                body="There is no dragging, no scrubbing, no keyframes. Intent in, cut out."
              />
              <Highlight
                icon={<Users className="size-5" />}
                title="Consensus built in"
                body="Admin policy lets one approver ship. Team policy needs everyone — one reject ends it."
              />
              <Highlight
                icon={<Film className="size-5" />}
                title="Live execution"
                body="Stage-by-stage job progress streams into the room as it renders."
              />
            </div>
          </div>
        </section>

        <section className="grid-noise">
          <div className="mx-auto max-w-3xl px-6 py-28 text-center">
            <h2 className="text-4xl sm:text-5xl">Say the edit out loud.</h2>
            <p className="mx-auto mt-5 max-w-md text-muted-foreground">
              No account, no password. Your identity is generated locally the moment you
              arrive.
            </p>
            <Link
              to="/rooms"
              className="bg-gradient-accent glow-accent mt-10 inline-flex items-center gap-2 rounded-full px-7 py-3.5 text-sm font-medium text-primary-foreground transition-transform duration-200 ease-out hover:scale-[1.03]"
            >
              Open your rooms <ArrowRight className="size-4" />
            </Link>
          </div>
        </section>
      </main>

      <footer className="border-t border-border/60">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-6 py-8 text-sm text-muted-foreground sm:flex-row">
          <div className="flex items-center gap-2.5">
            <BrandMark className="size-7" />
            <Wordmark className="text-base" />
          </div>
          <p>Conversational video editing.</p>
        </div>
      </footer>
    </div>
  );
}

function ChatPreviewRow({
  align,
  label,
  text,
  ai,
}: {
  align: "left" | "right";
  label: string;
  text: string;
  ai?: boolean;
}) {
  return (
    <div className={align === "right" ? "flex justify-end" : "flex justify-start"}>
      <div className="max-w-lg">
        <p
          className={`mb-1.5 text-[11px] tracking-wide uppercase ${ai ? "text-ai" : "text-muted-foreground"} ${align === "right" ? "text-right" : ""}`}
        >
          {label}
        </p>
        <div
          className={
            align === "right"
              ? "bg-gradient-accent rounded-2xl rounded-br-md px-4 py-3 text-sm text-primary-foreground"
              : "rounded-2xl rounded-bl-md border border-ai/25 bg-ai/10 px-4 py-3 text-sm text-foreground"
          }
        >
          {text}
        </div>
      </div>
    </div>
  );
}

function Highlight({
  icon,
  title,
  body,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
}) {
  return (
    <div className="panel p-6">
      <span className="grid size-10 place-items-center rounded-xl border border-primary/25 bg-primary/10 text-primary">
        {icon}
      </span>
      <h3 className="mt-4 text-base">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body}</p>
    </div>
  );
}
