"use client";

import { useEffect, useRef, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

export default function SearchControls({
  initialQuery,
  initialSort,
}: {
  initialQuery: string;
  initialSort: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [pending, startTransition] = useTransition();
  const [value, setValue] = useState(initialQuery);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Keep the input in sync if the URL changes externally (e.g. back button).
  useEffect(() => {
    setValue(initialQuery);
  }, [initialQuery]);

  function pushParams(next: { q?: string; sort?: string }) {
    const params = new URLSearchParams(searchParams.toString());
    if (next.q !== undefined) {
      const q = next.q.trim();
      if (q) params.set("q", q);
      else params.delete("q");
    }
    if (next.sort !== undefined) params.set("sort", next.sort);
    params.delete("page"); // any new query/sort resets to page 1
    const qs = params.toString();
    startTransition(() => {
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    });
  }

  function onSearchChange(v: string) {
    setValue(v);
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => pushParams({ q: v }), 250);
  }

  return (
    <div className="controls">
      <div className="search">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="11" cy="11" r="8" />
          <path d="m21 21-4.3-4.3" />
        </svg>
        <input
          type="search"
          value={value}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search by email, name, topic or content…"
          autoComplete="off"
          aria-label="Search emails"
        />
        {pending && <span className="spin-mini" aria-hidden="true" />}
      </div>
      <select
        className="sort"
        value={initialSort}
        onChange={(e) => pushParams({ sort: e.target.value })}
        aria-label="Sort order"
      >
        <optgroup label="Date">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </optgroup>
        <optgroup label="Scrape run">
          <option value="run_desc">Step — latest run first</option>
          <option value="run_asc">Step — first run first</option>
        </optgroup>
        <optgroup label="Name">
          <option value="name_asc">Name A–Z</option>
          <option value="name_desc">Name Z–A</option>
        </optgroup>
        <optgroup label="Email">
          <option value="email_asc">Email A–Z</option>
          <option value="email_desc">Email Z–A</option>
        </optgroup>
        <optgroup label="Country">
          <option value="country_asc">Country A–Z</option>
          <option value="country_desc">Country Z–A</option>
        </optgroup>
      </select>
    </div>
  );
}
