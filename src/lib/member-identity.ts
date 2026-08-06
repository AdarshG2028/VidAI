const ADJECTIVES = [
  "Amber","Bold","Calm","Crisp","Deep","Eager","Fluid","Golden","Hazy","Iron",
  "Jade","Keen","Lucid","Mellow","Noble","Onyx","Prime","Quiet","Rapid","Solar",
  "Tidal","Umber","Vivid","Warm","Xenial","Young","Zesty","Brisk","Clever","Dusky",
];

const NOUNS = [
  "Falcon","Harbor","Lantern","Meadow","Nebula","Orbit","Pixel","Quartz","Ridge","Signal",
  "Tundra","Vector","Willow","Zephyr","Anchor","Beacon","Cinder","Delta","Ember","Frost",
  "Glacier","Horizon","Ion","Juniper","Kite","Lumen","Mosaic","Nova","Opal","Prism",
];

function hash(str: string): number {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export type MemberIdentity = {
  label: string;
  initials: string;
  hue: number;
  color: string;
  soft: string;
  border: string;
};

export function memberIdentity(userId: string): MemberIdentity {
  const h = hash(userId || "anonymous");
  const adj = ADJECTIVES[h % ADJECTIVES.length] ?? "Vivid";
  const noun = NOUNS[Math.floor(h / ADJECTIVES.length) % NOUNS.length] ?? "Signal";
  const hue = (h >>> 8) % 360;
  return {
    label: `${adj} ${noun}`,
    initials: `${adj[0]}${noun[0]}`,
    hue,
    color: `oklch(0.78 0.14 ${hue})`,
    soft: `oklch(0.78 0.14 ${hue} / 0.14)`,
    border: `oklch(0.78 0.14 ${hue} / 0.35)`,
  };
}

export function shortId(id: string) {
  return id ? `${id.slice(0, 8)}…${id.slice(-4)}` : "";
}