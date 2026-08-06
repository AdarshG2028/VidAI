import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  action,
  children,
  className,
  bodyClassName,
}: {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cn(
        "panel flex min-h-0 flex-col overflow-hidden transition-colors duration-300",
        className,
      )}
    >
      {title ? (
        <header className="flex items-start justify-between gap-3 border-b border-border/70 px-5 py-4">
          <div className="min-w-0">
            <h2 className="font-display text-[0.95rem] tracking-tight">{title}</h2>
            {subtitle ? (
              <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                {subtitle}
              </p>
            ) : null}
          </div>
          {action}
        </header>
      ) : null}
      <div className={cn("min-h-0 flex-1 overflow-y-auto p-5", bodyClassName)}>
        {children}
      </div>
    </section>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="animate-rise flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="relative mb-5">
        <div className="absolute inset-0 -z-10 rounded-full bg-primary/25 blur-2xl" />
        <div className="animate-float grid size-16 place-items-center rounded-2xl border border-border bg-surface-2/70 text-primary">
          {icon}
        </div>
      </div>
      <h3 className="font-display text-base">{title}</h3>
      <p className="mt-2 max-w-[34ch] text-sm leading-relaxed text-muted-foreground">
        {description}
      </p>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

const TONES: Record<string, string> = {
  neutral: "border-border bg-surface-2/60 text-muted-foreground",
  accent: "border-primary/35 bg-primary/12 text-primary",
  success: "border-success/35 bg-success/12 text-success",
  warning: "border-warning/35 bg-warning/12 text-warning",
  danger: "border-destructive/35 bg-destructive/12 text-destructive",
  info: "border-ai/35 bg-ai/12 text-ai",
};

export function Pill({
  children,
  tone = "neutral",
  pulse,
  className,
}: {
  children: ReactNode;
  tone?: keyof typeof TONES | string;
  pulse?: boolean;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium tracking-wide uppercase",
        TONES[tone] ?? TONES["neutral"],
        className,
      )}
    >
      {pulse ? (
        <span className="relative flex size-1.5">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-current opacity-70" />
          <span className="relative inline-flex size-1.5 rounded-full bg-current" />
        </span>
      ) : null}
      {children}
    </span>
  );
}

export function statusTone(status?: string) {
  const s = (status ?? "").toLowerCase();
  if (["succeeded", "completed", "analyzed", "approved", "done"].includes(s))
    return "success";
  if (["failed", "error", "rejected", "cancelled", "canceled"].includes(s))
    return "danger";
  if (["analyzing", "running", "pending", "queued", "in_progress"].includes(s))
    return "accent";
  return "neutral";
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div className={cn("shimmer rounded-lg bg-surface-2/70", className)} aria-hidden />
  );
}