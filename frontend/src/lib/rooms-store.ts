const KEY = "vedai.rooms";

export function getKnownRooms(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function rememberRoom(id: string) {
  if (typeof window === "undefined") return;
  const rooms = getKnownRooms().filter((r) => r !== id);
  rooms.unshift(id);
  window.localStorage.setItem(KEY, JSON.stringify(rooms));
}

export function forgetRoom(id: string) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    KEY,
    JSON.stringify(getKnownRooms().filter((r) => r !== id)),
  );
}