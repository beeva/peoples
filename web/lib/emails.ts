import "server-only";

export type SourceKey = "all" | "discourse" | "devto" | "aboutme" | string;

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

export interface QueryResult {
  items: EmailRecord[];
  total: number;
  page: number;
  perPage: number;
  totalPages: number;
  source: SourceKey;
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
): Promise<QueryResult> {
  const params = new URLSearchParams({
    source,
    page: String(page),
    per_page: String(perPage),
    sort,
  });
  if (q) params.set("q", q);

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
      stats: EMPTY_STATS,
      sources: FALLBACK_SOURCES,
      error: `Could not reach the data server at ${API_BASE_URL} (${message}).`,
    };
  }
}
