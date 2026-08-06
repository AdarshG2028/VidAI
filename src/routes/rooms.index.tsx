import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import {
  ArrowRight,
  Clapperboard,
  LogIn,
  Plus,
  RefreshCw,
  Trash2,
  Users,
  Video,
} from "lucide-react";
import { toast } from "sonner";
import { BrandMark, Wordmark } from "@/components/BrandMark";
import { EmptyState, Pill, Skeleton } from "@/components/room/primitives";
import { api, ApiError, type Snapshot } from "@/lib/api";
import { getUserId } from "@/lib/identity";
import { memberIdentity, shortId } from "@/lib/member-identity";
import { forgetRoom, getKnownRooms, rememberRoom } from "@/lib/rooms-store";

export const Route = createFileRoute("/rooms")({
  head: () => ({
    meta: [
      { title: "Your rooms — VedAI" },
      {
        name: "description",
        content:
          "Create a VedAI room or join one by ID to start editing video through conversation with your team.",
      },
      { property: "og:title", content: "Your rooms — VedAI" },
      {
        property: "og:description",
        content: "Create or join a collaborative AI video editing room.",
      },
    ],
  }),
  component: Dashboard,
});

type RoomCard =
  | { id: string; state: "loading" }
  | { id: string; state: "error"; message: string }
  | { id: string; state: "ready"; snapshot: Snapshot };

function Dashboard() {
  const navigate = useNavigate();
  const [userId, setUserId] = useState("");
  const [rooms, setRooms] = useState<RoomCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [joinId, setJoinId] = useState("");
  const [creating, setCreating] = useState(false);
  const [joining, setJoining] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const ids = getKnownRooms();
    setRooms(ids.map((id) => ({ id, state: "loading" }) as RoomCard));
    const results = await Promise.all(
      ids.map(async (id): Promise<RoomCard> => {
        try {
          return { id, state: "ready", snapshot: await api.getSnapshot(id) };
        } catch (err) {
          return {
            id,
            state: "error",
            message: err instanceof ApiError ? err.message : "Unavailable",
          };
        }
      }),
    );
    setRooms(results);
    setLoading(false);
  }, []);

  useEffect(() => {
    setUserId(getUserId());
    void load();
  }, [load]);

  const me = memberIdentity(userId);

  async function createRoom(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    try {
      const project = await api.createProject(name.trim() || undefined);
      rememberRoom(project.id);
      toast.success("Room created");
      void navigate({ to: "/rooms/$id", params: { id: project.id } });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not create the room");
    } finally {
      setCreating(false);
    }
  }

  async function joinRoom(e: React.FormEvent) {
    e.preventDefault();
    const id = joinId.trim();
    if (!id) return;
    setJoining(true);
    try {
      await api.joinProject(id);
      rememberRoom(id);
      toast.success("Joined room");
      void navigate({ to: "/rooms/$id", params: { id } });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not join that room");
    } finally {
      setJoining(false);
    }
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-40 border-b border-border/60 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-6">
          <Link to="/" className="flex items-center gap-3">
            <BrandMark />
            <Wordmark />
          </Link>
          <div className="flex items-center gap-3">
            <button
              onClick={() => void load()}
              className="rounded-full border border-border p-2 text-muted-foreground transition-colors duration-200 hover:border-primary/40 hover:text-foreground"
              aria-label="Refresh rooms"
            >
              <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <div className="flex items-center gap-2.5 rounded-full border border-border bg-surface/70 py-1.5 pr-4 pl-1.5">
              <span
                className="grid size-7 place-items-center rounded-full text-[11px] font-semibold"
                style={{ background: me.soft, color: me.color, border: `1px solid ${me.border}` }}
              >
                {me.initials}
              </span>
              <div className="leading-tight">
                <p className="text-xs font-medium">{me.label}</p>
                <p className="font-mono text-[10px] text-muted-foreground">
                  {shortId(userId)}
                </p>
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-12">
        <div className="animate-rise">
          <h1 className="text-4xl">Your rooms</h1>
          <p className="mt-3 max-w-xl text-muted-foreground">
            Rooms are tracked locally on this device — there's no server-side list. Keep
            the room ID if you want to come back from another browser.
          </p>
        </div>

        <div className="mt-10 grid gap-5 md:grid-cols-2">
          <form onSubmit={createRoom} className="panel glow-accent p-6">
            <div className="flex items-center gap-2 text-primary">
              <Plus className="size-4" />
              <h2 className="text-base">New room</h2>
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              You'll be the owner — you set the approval policy and invite members.
            </p>
            <div className="mt-5 flex flex-col gap-3 sm:flex-row">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Room name (optional)"
                className="flex-1 rounded-xl border border-input bg-background/60 px-4 py-2.5 text-sm outline-none transition-colors duration-200 placeholder:text-muted-foreground focus:border-primary/50"
              />
              <button
                type="submit"
                disabled={creating}
                className="bg-gradient-accent inline-flex items-center justify-center gap-2 rounded-xl px-5 py-2.5 text-sm font-medium text-primary-foreground transition-transform duration-200 ease-out hover:scale-[1.02] disabled:opacity-60"
              >
                {creating ? "Creating…" : "Create"}
                <ArrowRight className="size-4" />
              </button>
            </div>
          </form>

          <form onSubmit={joinRoom} className="panel p-6">
            <div className="flex items-center gap-2 text-muted-foreground">
              <LogIn className="size-4" />
              <h2 className="text-base text-foreground">Join by ID</h2>
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              Paste a room ID someone shared with you.
            </p>
            <div className="mt-5 flex flex-col gap-3 sm:flex-row">
              <input
                value={joinId}
                onChange={(e) => setJoinId(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className="flex-1 rounded-xl border border-input bg-background/60 px-4 py-2.5 font-mono text-xs outline-none transition-colors duration-200 placeholder:text-muted-foreground focus:border-primary/50"
              />
              <button
                type="submit"
                disabled={joining || !joinId.trim()}
                className="rounded-xl border border-border px-5 py-2.5 text-sm transition-colors duration-200 hover:border-primary/40 disabled:opacity-50"
              >
                {joining ? "Joining…" : "Join"}
              </button>
            </div>
          </form>
        </div>

        <div className="mt-14">
          <div className="flex items-center justify-between">
            <h2 className="text-lg">Tracked rooms</h2>
            <span className="text-xs text-muted-foreground">{rooms.length} on this device</span>
          </div>

          {rooms.length === 0 ? (
            <div className="panel mt-6">
              <EmptyState
                icon={<Clapperboard className="size-7" />}
                title="No rooms yet"
                description="Create your first room above, or join one with an ID. Everything you touch gets remembered here."
              />
            </div>
          ) : (
            <div className="mt-6 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {rooms.map((room, i) => (
                <RoomTile
                  key={room.id}
                  room={room}
                  userId={userId}
                  index={i}
                  onForget={() => {
                    forgetRoom(room.id);
                    void load();
                  }}
                />
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

function RoomTile({
  room,
  userId,
  index,
  onForget,
}: {
  room: RoomCard;
  userId: string;
  index: number;
  onForget: () => void;
}) {
  if (room.state === "loading") {
    return (
      <div className="panel space-y-3 p-5">
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-1/3" />
      </div>
    );
  }

  if (room.state === "error") {
    return (
      <div className="panel animate-rise flex flex-col gap-3 border-destructive/25 p-5">
        <p className="font-mono text-xs text-muted-foreground">{shortId(room.id)}</p>
        <p className="text-sm text-destructive">{room.message}</p>
        <button
          onClick={onForget}
          className="inline-flex w-fit items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <Trash2 className="size-3.5" /> Forget this room
        </button>
      </div>
    );
  }

  const s = room.snapshot;
  const isOwner = s.project?.owner_id === userId;
  return (
    <Link
      to="/rooms/$id"
      params={{ id: room.id }}
      className="panel animate-rise group flex flex-col justify-between p-5 transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-primary/35"
      style={{ animationDelay: `${index * 50}ms` }}
    >
      <div>
        <div className="flex items-start justify-between gap-3">
          <h3 className="text-base leading-snug">{s.project?.name || "Untitled room"}</h3>
          {isOwner ? <Pill tone="accent">Owner</Pill> : null}
        </div>
        <p className="mt-1.5 font-mono text-[11px] text-muted-foreground">
          {shortId(room.id)}
        </p>
      </div>
      <div className="mt-6 flex items-center gap-4 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Users className="size-3.5" /> {s.members?.length ?? 0}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Video className="size-3.5" /> {s.videos?.length ?? 0}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Clapperboard className="size-3.5" /> {s.exports?.length ?? 0}
        </span>
        <ArrowRight className="ml-auto size-4 text-primary opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
      </div>
    </Link>
  );
}