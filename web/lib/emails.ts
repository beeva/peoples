import "server-only";

export type SourceKey = "all" | "discourse" | "devto" | "aboutme" | string;

export interface PostRef {
  title: string;
  url: string;
  createdAt: string;
}

export interface EmailRecord {
  id: string;
  source: SourceKey;
  emails: string[];
  name: string;
  username: string;
  title: string;
  url: string;
  createdAt: string;
  preview: string;
  tags: string[];
  location: string;
  organization: string;
  applyLinks: string[];
  messaging: string[];
  links: string[];
  posts: PostRef[];
  postCount: number;
  country: string;
  countryCode: string;
  gender: string;
  messaged: boolean;
  messagedCount: number;
  messagedAt: string;
  messagedTo: string;
  /** True when the sent flag was set by hand, not by an actual send. */
  messagedManual: boolean;
}

export interface Stats {
  totalPosts: number;
  totalEmails: number;
  uniqueEmails: number;
  earliest: string | null;
  latest: string | null;
  noun: string;
}

export interface SourceInfo {
  key: SourceKey;
  label: string;
  noun: string;
  count: number;
}

export type MessagedKey = "sent" | "unsent";
/** Normalized selection: "all", a single key, or both keys joined. */
export type MessagedFilter = "all" | "sent" | "unsent" | "sent,unsent";

export interface MessagedCounts {
  all: number;
  sent: number;
  unsent: number;
}

/** Parse a raw `messaged` value (comma list) into a normalized filter. */
export function parseMessaged(v: string | undefined): MessagedFilter {
  const set = new Set(
    (v || "").split(",").map((s) => s.trim()).filter((s) => s === "sent" || s === "unsent"),
  );
  if (set.has("sent") && set.has("unsent")) return "sent,unsent";
  if (set.has("sent")) return "sent";
  if (set.has("unsent")) return "unsent";
  return "all";
}

export interface QueryResult {
  items: EmailRecord[];
  total: number;
  page: number;
  perPage: number;
  totalPages: number;
  source: SourceKey;
  messaged: MessagedFilter;
  messagedCounts: MessagedCounts;
  stats: Stats;
  sources: SourceInfo[];
  error: string | null;
}

export const PER_PAGE = 12;

/** Base URL of the Python data server (server.py). Override with API_BASE_URL. */
export const API_BASE_URL = (
  process.env.API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

const EMPTY_STATS: Stats = {
  totalPosts: 0,
  totalEmails: 0,
  uniqueEmails: 0,
  earliest: null,
  latest: null,
  noun: "Records",
};

const FALLBACK_SOURCES: SourceInfo[] = [
  { key: "all", label: "All", noun: "Records", count: 0 },
];

// ---- Raw shapes returned by the Python API (snake_case) ----
interface RawItem {
  id?: string;
  source?: string;
  emails?: string[];
  name?: string;
  username?: string;
  title?: string;
  url?: string;
  created_at?: string;
  preview?: string;
  tags?: string[];
  location?: string;
  organization?: string;
  apply_links?: string[];
  messaging?: string[];
  links?: string[];
  posts?: { title?: string; url?: string; created_at?: string }[];
  post_count?: number;
  country?: string;
  country_code?: string;
  gender?: string;
  messaged?: boolean;
  messaged_count?: number;
  messaged_at?: string;
  messaged_to?: string;
  messaged_manual?: boolean;
}

interface RawStats {
  total_posts?: number;
  total_emails?: number;
  unique_emails?: number;
  earliest?: string | null;
  latest?: string | null;
  noun?: string;
}

interface RawSource {
  key?: string;
  label?: string;
  noun?: string;
  count?: number;
}

interface RawResponse {
  items?: RawItem[];
  total?: number;
  page?: number;
  per_page?: number;
  total_pages?: number;
  source?: string;
  messaged?: string;
  messaged_counts?: { all?: number; sent?: number; unsent?: number };
  stats?: RawStats;
  sources?: RawSource[];
}

function mapItem(it: RawItem, idx: number): EmailRecord {
  return {
    id: it.id ?? String(idx),
    source: it.source ?? "all",
    emails: Array.isArray(it.emails) ? it.emails : [],
    name: (it.name ?? "").trim(),
    username: (it.username ?? "").trim(),
    title: it.title ?? "",
    url: it.url ?? "",
    createdAt: it.created_at ?? "",
    preview: it.preview ?? "",
    tags: Array.isArray(it.tags) ? it.tags : [],
    location: it.location ?? "",
    organization: it.organization ?? "",
    applyLinks: Array.isArray(it.apply_links) ? it.apply_links : [],
    messaging: Array.isArray(it.messaging) ? it.messaging : [],
    links: Array.isArray(it.links) ? it.links : [],
    posts: Array.isArray(it.posts)
      ? it.posts.map((p) => ({
          title: p.title ?? "",
          url: p.url ?? "",
          createdAt: p.created_at ?? "",
        }))
      : [],
    postCount: it.post_count ?? 1,
    country: it.country ?? "",
    countryCode: it.country_code ?? "",
    gender: it.gender ?? "",
    messaged: Boolean(it.messaged),
    messagedCount: it.messaged_count ?? 0,
    messagedAt: it.messaged_at ?? "",
    messagedTo: it.messaged_to ?? "",
    messagedManual: Boolean(it.messaged_manual),
  };
}

function mapStats(s: RawStats | undefined): Stats {
  if (!s) return EMPTY_STATS;
  return {
    totalPosts: s.total_posts ?? 0,
    totalEmails: s.total_emails ?? 0,
    uniqueEmails: s.unique_emails ?? 0,
    earliest: s.earliest ?? null,
    latest: s.latest ?? null,
    noun: s.noun ?? "Records",
  };
}

function mapSources(list: RawSource[] | undefined): SourceInfo[] {
  if (!list || list.length === 0) return FALLBACK_SOURCES;
  return list.map((s) => ({
    key: s.key ?? "all",
    label: s.label ?? "All",
    noun: s.noun ?? "Records",
    count: s.count ?? 0,
  }));
}

/** Fetch a page of records from the Python data server. */
export async function fetchEmails(
  source: SourceKey,
  q: string,
  sort: "newest" | "oldest",
  page: number,
  perPage: number = PER_PAGE,
  messaged: MessagedFilter = "all",
): Promise<QueryResult> {
  const params = new URLSearchParams({
    source,
    page: String(page),
    per_page: String(perPage),
    sort,
  });
  if (q) params.set("q", q);
  if (messaged !== "all") params.set("messaged", messaged);

  const url = `${API_BASE_URL}/api/emails?${params.toString()}`;

  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) {
      throw new Error(`Data server responded ${res.status}`);
    }
    const data: RawResponse = await res.json();
    return {
      items: (data.items ?? []).map(mapItem),
      total: data.total ?? 0,
      page: data.page ?? page,
      perPage: data.per_page ?? perPage,
      totalPages: data.total_pages ?? 1,
      source: data.source ?? source,
      messaged: parseMessaged(data.messaged ?? messaged),
      messagedCounts: {
        all: data.messaged_counts?.all ?? 0,
        sent: data.messaged_counts?.sent ?? 0,
        unsent: data.messaged_counts?.unsent ?? 0,
      },
      stats: mapStats(data.stats),
      sources: mapSources(data.sources),
      error: null,
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return {
      items: [],
      total: 0,
      page: 1,
      perPage,
      totalPages: 1,
      source,
      messaged,
      messagedCounts: { all: 0, sent: 0, unsent: 0 },
      stats: EMPTY_STATS,
      sources: FALLBACK_SOURCES,
      error: `Could not reach the data server at ${API_BASE_URL} (${message}).`,
    };
  }
}

// ---- Single-contact detail (all posts, full text) ----
export interface PostFull {
  title: string;
  url: string;
  createdAt: string;
  text: string;
}

export interface EmailDetail extends EmailRecord {
  postsFull: PostFull[];
}

interface RawDetail extends RawItem {
  posts_full?: { title?: string; url?: string; created_at?: string; text?: string }[];
}

/** Fetch one merged contact with every occurrence's full text. */
export async function fetchEmailDetail(id: string): Promise<EmailDetail | null> {
  const url = `${API_BASE_URL}/api/email?id=${encodeURIComponent(id)}`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`Data server responded ${res.status}`);
    const it: RawDetail = await res.json();
    const base = mapItem(it, 0);
    const postsFull: PostFull[] = Array.isArray(it.posts_full)
      ? it.posts_full.map((p) => ({
          title: p.title ?? "",
          url: p.url ?? "",
          createdAt: p.created_at ?? "",
          text: p.text ?? "",
        }))
      : [];
    return { ...base, postsFull };
  } catch {
    return null;
  }
}
