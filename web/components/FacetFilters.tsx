"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { AgeOp, DateOp, FacetFilter, Facets } from "@/lib/filters";
import { dateActive, hasActiveFilter } from "@/lib/filters";
import { flagEmoji } from "@/lib/display";
import MergeRuns from "./MergeRuns";

// Reachability pills. "On WhatsApp" is the stricter claim: the number was
// published as a WhatsApp contact, not merely found alongside one.
const REACH = [
  { key: "phone", label: "Has phone" },
  { key: "whatsapp", label: "On WhatsApp" },
] as const;

const GENDERS: { key: "male" | "female" | "unknown"; label: string }[] = [
  { key: "male", label: "Male" },
  { key: "female", label: "Female" },
  { key: "unknown", label: "Unknown" },
];

const AGE_OPS: { key: Exclude<AgeOp, "">; label: string }[] = [
  { key: "over", label: "Over" },
  { key: "equal", label: "Equal to" },
  { key: "less", label: "Less than" },
];

/** Remembers whether the panel was left open, so the choice survives a reload
 *  and a move between the contact and database pages. */
const OPEN_KEY = "facets:open";

/** Country / gender / step / account-age filters for the contact table.
 *
 *  The whole panel writes to the URL, so a filtered view is shareable and the
 *  server does the actual filtering. Each option shows how many contacts it
 *  matches (the facet count), computed over the current source + search.
 *
 *  Layout is one labelled row per condition so every group gets the full
 *  width -- the country list wraps over as many lines as it needs instead of
 *  being squeezed into a side column, and a search box narrows it. Every row
 *  together is taller than the table it filters, so the panel collapses to its
 *  header; the count beside the title says how many conditions are on while it
 *  is shut.
 */
export default function FacetFilters({
  filter,
  facets,
  showAge = true,
  mergeSource = "",
}: {
  filter: FacetFilter;
  facets: Facets;
  /** Account age only means "years on GitHub" for the GitHub source; other
   *  sources' dates are post dates, so the age control is hidden for them. */
  showAge?: boolean;
  /** Source whose steps can be merged, or "" to hide the control. Merging
   *  rewrites one source's data file, so it is offered only when that single
   *  source is the one on screen -- not under "All". */
  mergeSource?: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [, startTransition] = useTransition();

  // Local age inputs so typing feels instant; the list updates on commit.
  const [ageOp, setAgeOp] = useState<AgeOp>(filter.ageOp);
  const [ageValue, setAgeValue] = useState(filter.ageValue);
  useEffect(() => {
    setAgeOp(filter.ageOp);
    setAgeValue(filter.ageValue);
  }, [filter.ageOp, filter.ageValue]);

  // Same for the joined / last-active date pairs: the op can be picked before
  // the date exists, so each row keeps local halves and commits when whole.
  const [joinedOp, setJoinedOp] = useState<DateOp>(filter.joinedOp);
  const [joinedDate, setJoinedDate] = useState(filter.joinedDate);
  const [activeOp, setActiveOp] = useState<DateOp>(filter.activeOp);
  const [activeDate, setActiveDate] = useState(filter.activeDate);
  useEffect(() => {
    setJoinedOp(filter.joinedOp);
    setJoinedDate(filter.joinedDate);
    setActiveOp(filter.activeOp);
    setActiveDate(filter.activeDate);
  }, [filter.joinedOp, filter.joinedDate, filter.activeOp, filter.activeDate]);

  // Open by default only when something is already filtered -- a shared
  // filtered link should show what it is filtering by. The stored preference,
  // read after mount so the server and first client render agree, wins over
  // that once the user has expressed one.
  const [open, setOpen] = useState(() => hasActiveFilter(filter));
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(OPEN_KEY);
      if (saved !== null) setOpen(saved === "1");
    } catch {
      // Private mode / blocked storage: the default above is fine.
    }
  }, []);

  function toggleOpen() {
    setOpen((wasOpen) => {
      try {
        window.localStorage.setItem(OPEN_KEY, wasOpen ? "0" : "1");
      } catch {
        // Not being able to remember it doesn't stop it from opening.
      }
      return !wasOpen;
    });
  }

  // Country search narrows the (long) list; selected stay visible so a
  // choice can always be un-made.
  const [countryQuery, setCountryQuery] = useState("");
  const visibleCountries = useMemo(() => {
    const q = countryQuery.trim().toLowerCase();
    if (!q) return facets.countries;
    return facets.countries.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        c.code.toLowerCase() === q ||
        filter.countries.includes(c.name),
    );
  }, [facets.countries, countryQuery, filter.countries]);

  function apply(next: FacetFilter) {
    const params = new URLSearchParams(searchParams.toString());
    const set = (k: string, v: string) =>
      v ? params.set(k, v) : params.delete(k);
    set("country", next.countries.join(","));
    set("gender", next.genders.join(","));
    set("contactable", next.contactable.join(","));
    set("runs", next.runs.join(","));
    const ageOn = next.ageOp !== "" && next.ageValue.trim() !== "";
    set("age_op", ageOn ? next.ageOp : "");
    set("age", ageOn ? next.ageValue.trim() : "");
    const joinedOn = dateActive(next.joinedOp, next.joinedDate);
    set("joined_op", joinedOn ? next.joinedOp : "");
    set("joined_date", joinedOn ? next.joinedDate : "");
    const activeOn = dateActive(next.activeOp, next.activeDate);
    set("active_op", activeOn ? next.activeOp : "");
    set("active_date", activeOn ? next.activeDate : "");
    params.delete("page"); // any filter change resets to page 1
    const qs = params.toString();
    startTransition(() => {
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    });
  }

  function toggleIn(list: string[], value: string): string[] {
    return list.includes(value)
      ? list.filter((v) => v !== value)
      : [...list, value];
  }

  function commitAge(op: AgeOp, value: string) {
    // Only hit the server once we have both a comparison and a number, or when
    // clearing -- a half-typed "Over ___" shouldn't filter anything yet.
    const ready = op !== "" && value.trim() !== "";
    const cleared = op === "" && value.trim() === "";
    if (ready || cleared) apply({ ...filter, ageOp: op, ageValue: value.trim() });
  }

  /** Commit a joined/active date condition once both halves are set (or on
   *  clearing) -- a half-picked "After ___" shouldn't filter anything yet. */
  function commitDate(
    which: "joined" | "active",
    op: DateOp,
    date: string,
  ) {
    const ready = op !== "" && date !== "";
    const cleared = op === "" || date === "";
    if (!ready && !cleared) return;
    const [opKey, dateKey] =
      which === "joined"
        ? (["joinedOp", "joinedDate"] as const)
        : (["activeOp", "activeDate"] as const);
    apply({
      ...filter,
      [opKey]: ready ? op : "",
      [dateKey]: ready ? date : "",
    });
  }

  function clearAll() {
    setCountryQuery("");
    apply({
      countries: [],
      genders: [],
      runs: [],
      contactable: [],
      ageOp: "",
      ageValue: "",
      joinedOp: "",
      joinedDate: "",
      activeOp: "",
      activeDate: "",
    });
  }

  const genderCounts = facets.genders;
  const active = hasActiveFilter(filter);
  const activeCount =
    filter.countries.length +
    filter.genders.length +
    filter.contactable.length +
    filter.runs.length +
    (filter.ageOp && filter.ageValue ? 1 : 0) +
    (dateActive(filter.joinedOp, filter.joinedDate) ? 1 : 0) +
    (dateActive(filter.activeOp, filter.activeDate) ? 1 : 0);

  return (
    <div className={`facet-filters${open ? "" : " shut"}`}>
      <div className="facet-head">
        <button
          className="facet-title facet-toggle"
          type="button"
          onClick={toggleOpen}
          aria-expanded={open}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          Filters
          {activeCount > 0 && <span className="facet-active-n">{activeCount}</span>}
          {/* Chevron, pointing down when shut and up when open. */}
          <svg className="facet-chevron" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth={2.2} strokeLinecap="round"
               strokeLinejoin="round" aria-hidden="true">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        {showAge && open && (
          <span className="facet-hint" role="note">
            Filters both the list below <strong>and</strong> what <em>Rescrape</em>{" "}
            goes and collects.
          </span>
        )}
        {active && (
          <button className="facet-clear" onClick={clearAll} type="button">
            Clear all
          </button>
        )}
      </div>

      {open && (
      <>
        {showAge && (
          <div className="facet-row">
            <span className="facet-label">Account age</span>
            {/* Age + joined + last-active share one line: three small
                comparison controls don't each deserve a full row. */}
            <div className="facet-controls">
              <select
                className="facet-op"
                value={ageOp}
                onChange={(e) => {
                  const op = e.target.value as AgeOp;
                  setAgeOp(op);
                  commitAge(op, ageValue);
                }}
                aria-label="Age comparison"
              >
                <option value="">Any</option>
                {AGE_OPS.map((o) => (
                  <option key={o.key} value={o.key}>
                    {o.label}
                  </option>
                ))}
              </select>
              <input
                className="facet-age-value"
                type="number"
                min={0}
                step={1}
                inputMode="numeric"
                placeholder="yrs"
                value={ageValue}
                disabled={ageOp === ""}
                onChange={(e) => setAgeValue(e.target.value)}
                onBlur={() => commitAge(ageOp, ageValue)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitAge(ageOp, ageValue);
                }}
                aria-label="Age in years"
              />
              <span className="facet-unit">years on GitHub</span>

              <span className="facet-label facet-label-inline">Joined</span>
              <select
                className="facet-op"
                value={joinedOp}
                onChange={(e) => {
                  const op = e.target.value as DateOp;
                  setJoinedOp(op);
                  commitDate("joined", op, joinedDate);
                }}
                aria-label="Join-date comparison"
              >
                <option value="">Any time</option>
                <option value="after">After</option>
                <option value="before">Before</option>
              </select>
              <input
                className="facet-date"
                type="date"
                value={joinedDate}
                disabled={joinedOp === ""}
                onChange={(e) => {
                  setJoinedDate(e.target.value);
                  commitDate("joined", joinedOp, e.target.value);
                }}
                aria-label="Join date"
              />

              <span className="facet-label facet-label-inline">Last active</span>
              <select
                className="facet-op"
                value={activeOp}
                onChange={(e) => {
                  const op = e.target.value as DateOp;
                  setActiveOp(op);
                  commitDate("active", op, activeDate);
                }}
                aria-label="Last-active comparison"
              >
                <option value="">Any time</option>
                <option value="after">After</option>
                <option value="before">Before</option>
              </select>
              <input
                className="facet-date"
                type="date"
                value={activeDate}
                disabled={activeOp === ""}
                onChange={(e) => {
                  setActiveDate(e.target.value);
                  commitDate("active", activeOp, e.target.value);
                }}
                aria-label="Last-active date"
              />
            </div>
          </div>
        )}

        <div className="facet-row">
          <span className="facet-label">Gender</span>
          <div className="facet-controls">
            {GENDERS.map((g) => (
              <label
                key={g.key}
                className={`check-pill${filter.genders.includes(g.key) ? " on" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={filter.genders.includes(g.key)}
                  onChange={() =>
                    apply({ ...filter, genders: toggleIn(filter.genders, g.key) })
                  }
                />
                <span>{g.label}</span>
                <span className="check-count">
                  {(genderCounts[g.key] ?? 0).toLocaleString()}
                </span>
              </label>
            ))}
          </div>
        </div>

        <div className="facet-row">
          <span className="facet-label">Reach</span>
          <div className="facet-controls">
            {REACH.map((r) => (
              <label
                key={r.key}
                className={`check-pill${filter.contactable.includes(r.key) ? " on" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={filter.contactable.includes(r.key)}
                  onChange={() =>
                    apply({
                      ...filter,
                      contactable: toggleIn(filter.contactable, r.key),
                    })
                  }
                />
                <span>{r.label}</span>
                <span className="check-count">
                  {(facets.contactable?.[r.key] ?? 0).toLocaleString()}
                </span>
              </label>
            ))}
          </div>
        </div>

        {facets.runs.length > 0 && (
          <div className="facet-row">
            <span className="facet-label">Step</span>
            <div className="facet-controls">
              {facets.runs.map((r) => {
                const key = String(r.run);
                const on = filter.runs.includes(key);
                return (
                  <label key={r.run} className={`check-pill${on ? " on" : ""}`}>
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() =>
                        apply({ ...filter, runs: toggleIn(filter.runs, key) })
                      }
                    />
                    <span>Run {r.run}</span>
                    <span className="check-count">{r.count.toLocaleString()}</span>
                  </label>
                );
              })}
              {mergeSource && (
                <MergeRuns source={mergeSource} runs={facets.runs} />
              )}
            </div>
          </div>
        )}

        <div className="facet-row">
          <span className="facet-label">
            Country
            {filter.countries.length > 0 && (
              <span className="facet-active-n">{filter.countries.length}</span>
            )}
          </span>
          <div className="facet-controls facet-countries">
            <input
              className="facet-search"
              type="search"
              placeholder="Search countries…"
              value={countryQuery}
              onChange={(e) => setCountryQuery(e.target.value)}
              aria-label="Search countries"
            />
            <div className="facet-country-list">
              {visibleCountries.length === 0 && (
                <span className="facet-empty">
                  {facets.countries.length === 0
                    ? "No countries yet"
                    : "No match"}
                </span>
              )}
              {visibleCountries.map((c) => (
                <label
                  key={c.name}
                  className={`check-pill${filter.countries.includes(c.name) ? " on" : ""}`}
                >
                  <input
                    type="checkbox"
                    checked={filter.countries.includes(c.name)}
                    onChange={() =>
                      apply({
                        ...filter,
                        countries: toggleIn(filter.countries, c.name),
                      })
                    }
                  />
                  <span>
                    {flagEmoji(c.code)} {c.name}
                  </span>
                  <span className="check-count">{c.count.toLocaleString()}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
      </>
      )}
    </div>
  );
}
