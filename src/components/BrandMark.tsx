import { cn } from "@/lib/utils";

export function BrandMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "relative grid size-9 shrink-0 place-items-center overflow-hidden rounded-xl border border-primary/30",
        className,
      )}
    >
      <span className="bg-gradient-accent absolute inset-0 opacity-90" />
      <svg
        viewBox="0 0 24 24"
        className="relative size-5 text-primary-foreground"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M4 5.5 12 19l8-13.5" />
        <path d="M9.5 5.5h5" />
      </svg>
    </span>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn("font-display text-lg tracking-tight", className)}>
      Vid<span className="text-gradient">AI</span>
    </span>
  );
}