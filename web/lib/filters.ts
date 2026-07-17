// Country / gender / account-age filter model. No "server-only" guard here, on
// purpose: these types and pure helpers are shared by the client filter UI and
// the server data layer, so both sides agree on how a filter is shaped and how
// an age comparison maps to the (age_min, age_max) the API takes.

/** A country with how many contacts are in it, for the country filter. */
export interface CountryFacet {
  name: string;
  code: string;
  count: number;
}

export interface Facets {
  countries: CountryFacet[];
  genders: { male: number; female: number; unknown: number };
  /** Distribution across the fixed age bands, keyed lt1/1to3/3to5/5to10/gte10. */
  ages: Record<string, number>;
  /** Contacts per scrape run ("step"); only runs > 0 are listed. */
  runs: { run: number; count: number }[];
}

/** Compare account age (years on GitHub) against a single value. */
export type AgeOp = "" | "over" | "equal" | "less";

/** Compare a date field against a calendar day ("" = not filtering). */
export type DateOp = "" | "after" | "before";

/** The country/gender/step/age/date filter selection, as it travels in the URL. */
export interface FacetFilter {
  countries: string[]; // country names or codes
  genders: string[]; // male | female | unknown
  runs: string[]; // scrape run numbers ("steps"), multi
  ageOp: AgeOp; // over N / exactly N / less than N years old
  ageValue: string; // whole years; "" when ageOp is ""
  joinedOp: DateOp; // joined after/before a date
  joinedDate: string; // ISO date; "" when joinedOp is ""
  activeOp: DateOp; // last publicly active after/before a date
  activeDate: string; // ISO date; "" when activeOp is ""
}

export const EMPTY_FILTER: FacetFilter = {
  countries: [],
  genders: [],
  runs: [],
  ageOp: "",
  ageValue: "",
  joinedOp: "",
  joinedDate: "",
  activeOp: "",
  activeDate: "",
};

/** Whether a date comparison is actually set (an op *and* a date). */
export function dateActive(op: DateOp, date: string): boolean {
  return op !== "" && date.trim() !== "";
}

/** Whether an age comparison is actually set (an op *and* a number). */
export function ageActive(f: FacetFilter): boolean {
  return f.ageOp !== "" && f.ageValue.trim() !== "";
}

export function hasActiveFilter(f: FacetFilter): boolean {
  return (
    f.countries.length > 0 ||
    f.genders.length > 0 ||
    f.runs.length > 0 ||
    ageActive(f) ||
    dateActive(f.joinedOp, f.joinedDate) ||
    dateActive(f.activeOp, f.activeDate)
  );
}

/** Turn "over 5 / exactly 5 / less than 5" into the (age_min, age_max) the API
 *  takes. Bounds are in years; min is inclusive, max exclusive. "equal N" means
 *  the whole year [N, N+1) -- i.e. an account that is N years old. */
export function ageToRange(f: FacetFilter): { min: string; max: string } {
  if (!ageActive(f)) return { min: "", max: "" };
  const n = Number(f.ageValue);
  if (!Number.isFinite(n)) return { min: "", max: "" };
  if (f.ageOp === "over") return { min: String(n), max: "" };
  if (f.ageOp === "less") return { min: "", max: String(n) };
  return { min: String(n), max: String(n + 1) }; // equal
}

function csvList(v: string | string[] | undefined): string[] {
  const raw = Array.isArray(v) ? v.join(",") : v || "";
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

/** Read the country/gender/step/age/date filter out of the page's search params. */
export function parseFilter(sp: {
  country?: string | string[];
  gender?: string | string[];
  runs?: string | string[];
  age_op?: string | string[];
  age?: string | string[];
  joined_op?: string | string[];
  joined_date?: string | string[];
  active_op?: string | string[];
  active_date?: string | string[];
}): FacetFilter {
  const first = (v: string | string[] | undefined) =>
    (Array.isArray(v) ? v[0] : v) || "";
  const genders = csvList(sp.gender).filter((g) =>
    ["male", "female", "unknown"].includes(g),
  );
  const opRaw = first(sp.age_op).trim();
  const ageOp: AgeOp = (["over", "equal", "less"] as const).includes(
    opRaw as "over",
  )
    ? (opRaw as AgeOp)
    : "";
  const ageValue = first(sp.age).trim();
  // A date pair only counts when both halves are present and sane.
  const datePair = (
    opV: string | string[] | undefined,
    dateV: string | string[] | undefined,
  ): { op: DateOp; date: string } => {
    const op = first(opV).trim();
    const date = first(dateV).trim();
    const ok =
      (op === "after" || op === "before") && /^\d{4}-\d{2}-\d{2}$/.test(date);
    return ok ? { op: op as DateOp, date } : { op: "", date: "" };
  };
  const joined = datePair(sp.joined_op, sp.joined_date);
  const active = datePair(sp.active_op, sp.active_date);
  return {
    countries: csvList(sp.country),
    genders,
    runs: csvList(sp.runs).filter((r) => /^\d+$/.test(r)),
    ageOp: ageValue ? ageOp : "",
    ageValue: ageOp ? ageValue : "",
    joinedOp: joined.op,
    joinedDate: joined.date,
    activeOp: active.op,
    activeDate: active.date,
  };
}
