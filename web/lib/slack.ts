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

export interface WorkspaceData {
  slug: string;
  name: string;
  url: string;
  columns: Column[];
  rows: Record<string, unknown>[];
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

// Preferred column order; anything else is appended alphabetically.
const PRIORITY = [
  "name",
  "real_name",
  "profile.email",
  "profile.title",
  "profile.phone",
  "profile.display_name",
  "profile.first_name",
  "profile.last_name",
  "profile.status_text",
  "tz_label",
  "tz",
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

// Full keys that duplicate another column (top-level wins).
const DENY_KEYS = new Set(["profile.real_name"]);

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
function flattenUser(u: Json): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(u)) {
    if (k === "profile" || k === "workspace") continue;
    if (isScalar(v) && !isDenied(k)) out[k] = v;
  }
  const profile = (u.profile ?? {}) as Json;
  for (const [k, v] of Object.entries(profile)) {
    const key = `profile.${k}`;
    if (isScalar(v) && !isDenied(key)) out[key] = v;
  }
  // Avatar + display name are rendered specially, kept off the column list.
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

/** Load one workspace: derive columns from its JSON and flatten every user. */
export async function loadWorkspace(
  slug: string,
): Promise<WorkspaceData | null> {
  const all = await listWorkspaces();
  const info = all.find((w) => w.slug === slug);
  if (!info) return null;

  const users = await readJson(info.file);
  const rows = users.map(flattenUser);
  const ws = (users[0]?.workspace ?? {}) as Json;

  // Candidate columns = every key that has a non-empty value on some row.
  const nonEmpty = new Set<string>();
  for (const row of rows) {
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

  const columns: Column[] = ordered.map((key) => ({
    key,
    label: labelFor(key),
    // Booleans are detected from the data so we can render check/blank cells.
    kind: rows.some((r) => typeof r[key] === "boolean")
      ? "bool"
      : kindFor(key),
  }));

  return {
    slug: info.slug,
    name: info.name,
    url: (ws.url as string) || "",
    columns,
    rows,
  };
}
