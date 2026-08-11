import "server-only";
import {
  ageToRange,
  EMPTY_FILTER,
  type FacetFilter,
  type Facets,
} from "./filters";

// Re-export the filter model so server-side callers can keep importing it from
// "@/lib/emails"; the client imports it straight from "@/lib/filters" (which has
// no server-only guard).
export type { AgeOp, CountryFacet, DateOp, FacetFilter, Facets } from "./filters";
export {
  ageActive,
  ageToRange,
  dateActive,
  EMPTY_FILTER,
  hasActiveFilter,
  parseFilter,
} from "./filters";

export type SourceKey =
  | "all"
  | "discourse"
  | "devto"
  | "aboutme"
  | "github"
  | string;

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
  /** The person's own site (GitHub `blog`), distinct from the profile `url`. */
  siteUrl: string;
  createdAt: string;
  /** Last time the contact was publicly active at the source ("" if unknown). */
  activityAt: string;
  /** Which scrape run collected this contact (0 = the source doesn't number runs). */
  run: number;
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
  /** Numbers found for this contact, best first (WhatsApp leads). */
  phones: Phone[];
  /** The one to message -- phones[0], or "" if there is none. */
  phone: string;
  phoneCount: number;
  /** At least one number was published *as* a WhatsApp contact. */
  hasWhatsapp: boolean;
  messaged: boolean;
  messagedCount: number;
  messagedAt: string;
  messagedTo: string;
  /** True when the sent flag was set by hand, not by an actual send. */
  messagedManual: boolean;
}

export interface Phone {
  number: string;
  /** Published as a WhatsApp contact, not merely a number that might work. */
  whatsapp: boolean;
  /** What vouched for it: wa-link, tel-link, label or intl. */
  via: string;
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
  filter: FacetFilter;
  facets: Facets;
  stats: Stats;
  sources: SourceInfo[];
  error: string | null;
}

export const PER_PAGE = 12;

/** Base URL of the Python data server (server.py). Override with API_BASE_URL. */
export const API_BASE_URL = (
  process.env.API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

/** Shared secret for the data server; empty in local development.
 *
 *  Deliberately not NEXT_PUBLIC_: every call to the data server is made from
 *  the Next.js server (route handlers and server components), never from the
 *  browser, so the token has no reason to leave this process -- and a
 *  NEXT_PUBLIC_ name would inline it into the client bundle for everyone.
 */
export const API_HEADERS: Record<string, string> = process.env.API_TOKEN
  ? { "X-Api-Token": process.env.API_TOKEN }
  : {};

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
  site_url?: string;
  created_at?: string;
  activity_at?: string;
  run?: number;
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
  phones?: { number?: string; whatsapp?: boolean; via?: string }[];
  phone?: string;
  phone_count?: number;
  has_whatsapp?: boolean;
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
  country?: string;
  gender?: string;
  runs?: string;
  age_min?: string;
  age_max?: string;
  facets?: {
    countries?: { name?: string; code?: string; count?: number }[];
    genders?: { male?: number; female?: number; unknown?: number };
    ages?: Record<string, number>;
    runs?: { run?: number; count?: number }[];
    contactable?: { phone?: number; whatsapp?: number };
  };
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
    siteUrl: it.site_url ?? "",
    createdAt: it.created_at ?? "",
    activityAt: it.activity_at ?? "",
    run: Number(it.run) || 0,
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
    phones: Array.isArray(it.phones)
      ? it.phones.map((p) => ({
          number: p.number ?? "",
          whatsapp: Boolean(p.whatsapp),
          via: p.via ?? "",
        }))
      : [],
    phone: it.phone ?? "",
    phoneCount: it.phone_count ?? 0,
    hasWhatsapp: Boolean(it.has_whatsapp),
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

/** Every column the table can be ordered by, and which way. Mirrors `SORTS` in
 *  server.py; "newest"/"oldest" keep their old names so existing links work. */
export const SORT_KEYS = [
  "newest",
  "oldest",
  "run_desc",
  "run_asc",
  "name_asc",
  "name_desc",
  "email_asc",
  "email_desc",
  "country_asc",
  "country_desc",
] as const;

export type SortKey = (typeof SORT_KEYS)[number];

export const DEFAULT_SORT: SortKey = "newest";

export function isSortKey(value: string | undefined): value is SortKey {
  return !!value && (SORT_KEYS as readonly string[]).includes(value);
}

/** Parse the server's echoed `runs` (a comma list of ints) back to a list. */
function parseRuns(v: string | undefined): string[] {
  return (v ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => /^\d+$/.test(s));
}

/** Fetch a page of records from the Python data server. */
export async function fetchEmails(
  source: SourceKey,
  q: string,
  sort: SortKey,
  page: number,
  perPage: number = PER_PAGE,
  messaged: MessagedFilter = "all",
  filter: FacetFilter = EMPTY_FILTER,
): Promise<QueryResult> {
  const params = new URLSearchParams({
    source,
    page: String(page),
    per_page: String(perPage),
    sort,
  });
  if (q) params.set("q", q);
  if (messaged !== "all") params.set("messaged", messaged);
  if (filter.countries.length) params.set("country", filter.countries.join(","));
  if (filter.genders.length) params.set("gender", filter.genders.join(","));
  if (filter.contactable.length)
    params.set("contactable", filter.contactable.join(","));
  if (filter.runs.length) params.set("runs", filter.runs.join(","));
  const { min, max } = ageToRange(filter);
  if (min) params.set("age_min", min);
  if (max) params.set("age_max", max);
  if (filter.joinedOp && filter.joinedDate) {
    params.set("joined_op", filter.joinedOp);
    params.set("joined_date", filter.joinedDate);
  }
  if (filter.activeOp && filter.activeDate) {
    params.set("active_op", filter.activeOp);
    params.set("active_date", filter.activeDate);
  }

  const url = `${API_BASE_URL}/api/emails?${params.toString()}`;

  try {
    const res = await fetch(url, { cache: "no-store", headers: API_HEADERS });
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
      // Echo the selection the caller asked for -- the server round-trips
      // country/gender but not the age *operator* (it only knows min/max), so
      // the client filter is the source of truth for the age comparison. Runs
      // are the exception: the server drops step numbers that no longer exist
      // (a run merged away leaves a stale `runs=` in the URL), so trust its
      // reconciled list to keep the facet count and links honest.
      filter: { ...filter, runs: parseRuns(data.runs) },
      facets: mapFacets(data.facets),
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
      filter,
      facets: EMPTY_FACETS,
      stats: EMPTY_STATS,
      sources: FALLBACK_SOURCES,
      error: `Could not reach the data server at ${API_BASE_URL} (${message}).`,
    };
  }
}

const EMPTY_FACETS: Facets = {
  countries: [],
  genders: { male: 0, female: 0, unknown: 0 },
  ages: {},
  runs: [],
  contactable: { phone: 0, whatsapp: 0 },
};

function mapFacets(f: RawResponse["facets"]): Facets {
  return {
    countries: (f?.countries ?? [])
      .map((c) => ({
        name: (c.name ?? "").trim(),
        code: (c.code ?? "").trim(),
        count: c.count ?? 0,
      }))
      .filter((c) => c.name),
    genders: {
      male: f?.genders?.male ?? 0,
      female: f?.genders?.female ?? 0,
      unknown: f?.genders?.unknown ?? 0,
    },
    ages: f?.ages ?? {},
    contactable: {
      phone: f?.contactable?.phone ?? 0,
      whatsapp: f?.contactable?.whatsapp ?? 0,
    },
    runs: (f?.runs ?? [])
      .map((r) => ({ run: r.run ?? 0, count: r.count ?? 0 }))
      .filter((r) => r.run > 0),
  };
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
    const res = await fetch(url, { cache: "no-store", headers: API_HEADERS });
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
