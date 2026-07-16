"use client";

import { useEffect, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { AgeOp, FacetFilter, Facets } from "@/lib/filters";
import { hasActiveFilter } from "@/lib/filters";
import { flagEmoji } from "@/lib/display";

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

/** Country / gender / account-age filters for the contact table.
 *
 *  The whole panel writes to the URL, so a filtered view is shareable and the
 *  server does the actual filtering. Each option shows how many contacts it
 *  matches (the facet count), computed over the current source + search.
 */
export default function FacetFilters({
  filter,
  facets,
  showAge = true,
}: {
  filter: FacetFilter;
  facets: Facets;
  /** Account age only means "years on GitHub" for the GitHub source; other
   *  sources' dates are post dates, so the age control is hidden for them. */
  showAge?: boolean;
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

  function apply(next: FacetFilter) {
    const params = new URLSearchParams(searchParams.toString());
    const set = (k: string, v: string) =>
      v ? params.set(k, v) : params.delete(k);
    set("country", next.countries.join(","));
    set("gender", next.genders.join(","));
    const ageOn = next.ageOp !== "" && next.ageValue.trim() !== "";
    set("age_op", ageOn ? next.ageOp : "");
    set("age", ageOn ? next.ageValue.trim() : "");
    params.delete("page"); // any filter change resets to page 1
    const qs = params.toString();
    startTransition(() => {
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    });
  }

  function toggleCountry(name: string) {
    const has = filter.countries.includes(name);
    apply({
      ...filter,
      countries: has
        ? filter.countries.filter((c) => c !== name)
        : [...filter.countries, name],
    });
  }

  function toggleGender(key: "male" | "female" | "unknown") {
    const has = filter.genders.includes(key);
    apply({
      ...filter,
      genders: has
        ? filter.genders.filter((g) => g !== key)
        : [...filter.genders, key],
    });
  }

  function commitAge(op: AgeOp, value: string) {
    // Only hit the server once we have both a comparison and a number, or when
    // clearing -- a half-typed "Over ___" shouldn't filter anything yet.
    const ready = op !== "" && value.trim() !== "";
    const cleared = op === "" && value.trim() === "";
    if (ready || cleared) apply({ ...filter, ageOp: op, ageValue: value.trim() });
  }

  function clearAll() {
    apply({ countries: [], genders: [], ageOp: "", ageValue: "" });
  }

  const genderCounts = facets.genders;
  const active = hasActiveFilter(filter);

  return (
    <div className="facet-filters">
      {showAge && (
        <div className="facet-hint" role="note">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          <span>
            Filters both the list below <strong>and</strong> what{" "}
            <em>Rescrape</em> goes and collects.
          </span>
        </div>
      )}
      {showAge && (
        <div className="facet-group facet-age">
          <span className="facet-label">Account age</span>
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
        </div>
      )}

      <div className="facet-group">
        <span className="facet-label">Gender</span>
        {GENDERS.map((g) => (
          <label
            key={g.key}
            className={`check-pill${filter.genders.includes(g.key) ? " on" : ""}`}
          >
            <input
              type="checkbox"
              checked={filter.genders.includes(g.key)}
              onChange={() => toggleGender(g.key)}
            />
            <span>{g.label}</span>
            <span className="check-count">
              {(genderCounts[g.key] ?? 0).toLocaleString()}
            </span>
          </label>
        ))}
      </div>

      <div className="facet-group facet-countries">
        <span className="facet-label">Country</span>
        <div className="facet-country-scroll">
          {facets.countries.length === 0 && (
            <span className="facet-empty">No countries yet</span>
          )}
          {facets.countries.map((c) => (
            <label
              key={c.name}
              className={`check-pill${filter.countries.includes(c.name) ? " on" : ""}`}
            >
              <input
                type="checkbox"
                checked={filter.countries.includes(c.name)}
                onChange={() => toggleCountry(c.name)}
              />
              <span>
                {flagEmoji(c.code)} {c.name}
              </span>
              <span className="check-count">{c.count.toLocaleString()}</span>
            </label>
          ))}
        </div>
      </div>

      {active && (
        <button className="facet-clear" onClick={clearAll} type="button">
          Clear filters
        </button>
      )}
    </div>
  );
}
