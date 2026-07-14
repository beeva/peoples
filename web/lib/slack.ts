import "server-only";

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { cache } from "react";

/** Directory holding one JSON file per Slack workspace export. */
const USERS_DIR =
  process.env.SLACK_USERS_DIR ||
  path.join(process.cwd(), "..", "scrapers", "slack", "users");

export interface WorkspaceInfo {
  slug: string; // filename without extension (URL id)
  file: string; // absolute path
  name: string; // human workspace name
  count: number; // number of users
}

export interface Column {
  key: string; // flat key, e.g. "name" or "profile.email"
  label: string; // header label derived from the key
  kind: "avatar" | "email" | "bool" | "date" | "id" | "text";
}

type Row = Record<string, unknown>;

export interface ServerRef {
  slug: string;
  name: string;
}

/** One user's full flattened record within a single workspace. */
export interface ServerData extends ServerRef {
  fields: Row;
}

/** A person, deduplicated across servers by email. */
export interface DisplayUser {
  key: string; // email (lowercased) or slug:id fallback
  name: string;
  email: string;
  avatar: string;
  freq: number; // how many servers they appear in
  servers: ServerRef[]; // every server they belong to
  serverData: ServerData[]; // per-server flattened records (for detail tabs)
  fields: Row; // representative record used for table columns
}

export interface ViewData {
  view: string; // "all" or a workspace slug
  name: string;
  count: number;
  columns: Column[];
  users: DisplayUser[];
}

type Json = Record<string, unknown>;

// Leaf field names we never want as columns (avatars, hashes, dup fields…).
const DENY_LEAVES = new Set([
  "avatar_hash",
  "color",
  "team_id",
  "team",
  "tz_offset",
  "is_app_user",
  "is_invited_user",
  "is_forgotten",
  "is_email_confirmed",
  "who_can_share_contact_card",
  "status_emoji",
  "status_emoji_display_info",
  "status_text_canonical",
  "status_expiration",
  "huddle_state",
  "huddle_state_expiration_ts",
  "is_custom_image",
  "skype",
]);

// Full keys that duplicate another column (top-level wins).
const DENY_KEYS = new Set(["profile.real_name"]);

// Preferred column order; anything else is appended alphabetically.
const PRIORITY = [
  "name",
  "real_name",
  "profile.email",
  "tz_label",
  "tz",
  "profile.phone",
  "profile.title",
  "profile.display_name",
  "profile.first_name",
  "profile.last_name",
  "profile.status_text",
  "is_admin",
  "is_owner",
  "is_primary_owner",
  "is_bot",
  "is_restricted",
  "is_ultra_restricted",
  "deleted",
  "updated",
  "id",
];

function isDenied(key: string): boolean {
  if (DENY_KEYS.has(key)) return true;
  const leaf = key.includes(".") ? key.split(".")[1] : key;
  if (/^image_/.test(leaf)) return true;
  if (/_normalized$/.test(leaf)) return true;
  return DENY_LEAVES.has(leaf);
}

function isScalar(v: unknown): v is string | number | boolean {
  return (
    typeof v === "string" || typeof v === "number" || typeof v === "boolean"
  );
}

/** Turn a flat key into a readable header label. */
function labelFor(key: string): string {
  const leaf = key.includes(".") ? key.split(".").slice(1).join(".") : key;
  return leaf
    .replace(/_/g, " ")
    .replace(/\bis (.+)/, "$1")
    .replace(/\btz\b/gi, "Timezone")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function kindFor(key: string): Column["kind"] {
  const leaf = key.includes(".") ? key.split(".")[1] : key;
  if (leaf === "email") return "email";
  if (key === "id") return "id";
  if (key === "updated" || /_ts$|_date$/.test(leaf)) return "date";
  return "text";
}

/** Flatten one Slack user into { key -> scalar } plus avatar/display helpers. */
function flattenUser(u: Json): Row {
  const out: Row = {};
  for (const [k, v] of Object.entries(u)) {
    if (k === "profile" || k === "workspace") continue;
    if (isScalar(v) && !isDenied(k)) out[k] = v;
  }
  const profile = (u.profile ?? {}) as Json;
  for (const [k, v] of Object.entries(profile)) {
    const key = `profile.${k}`;
    if (isScalar(v) && !isDenied(key)) out[key] = v;
  }
  out.__avatar =
    profile.image_192 ||
    profile.image_512 ||
    profile.image_72 ||
    profile.image_48 ||
    "";
  out.__name =
    (u.real_name as string) ||
    (profile.real_name as string) ||
    (u.name as string) ||
    (profile.display_name as string) ||
    "?";
  return out;
}

function readJson(file: string): Promise<Json[]> {
  return readFile(file, "utf-8").then((t) => JSON.parse(t) as Json[]);
}

/** List every workspace file with its display name + user count. */
export const listWorkspaces = cache(async (): Promise<WorkspaceInfo[]> => {
  let files: string[];
  try {
    files = (await readdir(USERS_DIR)).filter((f) => f.endsWith(".json"));
  } catch {
    return [];
  }
  const infos = await Promise.all(
    files.map(async (f) => {
      const file = path.join(USERS_DIR, f);
      const users = await readJson(file);
      const ws = (users[0]?.workspace ?? {}) as Json;
      return {
        slug: f.replace(/\.json$/, ""),
        file,
        name: (ws.name as string) || f.replace(/\.json$/, ""),
        count: users.length,
      };
    }),
  );
  infos.sort((a, b) => a.name.localeCompare(b.name));
  return infos;
});

/** Read every workspace and group users across servers by email. */
const buildIndex = cache(
  async (): Promise<{
    workspaces: WorkspaceInfo[];
    byKey: Map<string, ServerData[]>;
  }> => {
    const workspaces = await listWorkspaces();
    const byKey = new Map<string, ServerData[]>();
    for (const w of workspaces) {
      const users = await readJson(w.file);
      for (const u of users) {
        const fields = flattenUser(u);
        const email = String(fields["profile.email"] || "").toLowerCase();
        const key = email || `${w.slug}:${fields.id ?? ""}`;
        const entry: ServerData = { slug: w.slug, name: w.name, fields };
        const arr = byKey.get(key);
        if (arr) arr.push(entry);
        else byKey.set(key, [entry]);
      }
    }
    return { workspaces, byKey };
  },
);

/** Total number of unique people across all servers. */
export const countAllUsers = cache(async (): Promise<number> => {
  return (await buildIndex()).byKey.size;
});

/** Build one deduplicated person, preferring `primarySlug`'s record for display. */
function toDisplayUser(
  key: string,
  entries: ServerData[],
  primarySlug?: string,
): DisplayUser {
  const serverData = [...entries].sort((a, b) => a.name.localeCompare(b.name));
  const primary =
    (primarySlug && serverData.find((e) => e.slug === primarySlug)) ||
    serverData[0];
  const f = primary.fields;
  const avatar =
    String(f.__avatar || "") ||
    String(serverData.find((e) => e.fields.__avatar)?.fields.__avatar || "");
  return {
    key,
    name: String(f.__name || "?"),
    email: String(f["profile.email"] || ""),
    avatar,
    freq: serverData.length,
    servers: serverData.map((e) => ({ slug: e.slug, name: e.name })),
    serverData,
    fields: f,
  };
}

/** Derive the column set from the fields actually present on the given records. */
function deriveColumns(records: Row[]): Column[] {
  const nonEmpty = new Set<string>();
  for (const row of records) {
    for (const [k, v] of Object.entries(row)) {
      if (k.startsWith("__")) continue;
      if (v === "" || v === null || v === undefined) continue;
      nonEmpty.add(k);
    }
  }
  const ordered = [
    ...PRIORITY.filter((k) => nonEmpty.has(k)),
    ...[...nonEmpty].filter((k) => !PRIORITY.includes(k)).sort(),
  ];
  return ordered.map((key) => ({
    key,
    label: labelFor(key),
    kind: records.some((r) => typeof r[key] === "boolean")
      ? "bool"
      : kindFor(key),
  }));
}

/**
 * Load a view: "all" for the deduplicated cross-server directory, or a
 * workspace slug for that server's members (still annotated with every server
 * each person belongs to).
 */
export async function loadView(view: string): Promise<ViewData | null> {
  const { workspaces, byKey } = await buildIndex();
  const isAll = view === "all";
  const ws = isAll ? null : workspaces.find((w) => w.slug === view);
  if (!isAll && !ws) return null;

  const users: DisplayUser[] = [];
  for (const [key, entries] of byKey) {
    if (isAll) {
      users.push(toDisplayUser(key, entries));
    } else if (entries.some((e) => e.slug === view)) {
      users.push(toDisplayUser(key, entries, view));
    }
  }
  users.sort((a, b) => a.name.localeCompare(b.name));

  return {
    view,
    name: isAll ? "All Users" : ws!.name,
    count: users.length,
    columns: deriveColumns(users.map((u) => u.fields)),
    users,
  };
}
