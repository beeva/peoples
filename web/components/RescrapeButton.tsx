"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { SourceKey } from "@/lib/emails";
import { ageToRange, parseFilter } from "@/lib/filters";

interface JobStatus {
  status?: string; // idle | running | done | error
  last_run?: string | null;
  last_added?: number | null;
  total?: number | null;
  added?: number | null;
  message?: string;
  error?: string;
}

function relTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "never";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

export default function RescrapeButton({
  source,
  label,
}: {
  source: Exclude<SourceKey, "all">;
  label: string;
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [job, setJob] = useState<JobStatus>({ status: "idle" });
  const [busy, setBusy] = useState(false);
  // Pre-filled to the server's default batch size so the box shows a real
  // number, not just a placeholder; the user can overwrite it before scraping.
  const [target, setTarget] = useState("50");
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  const running = busy || job.status === "running";

  // What the active view filter will target when scraping (GitHub only). The
  // filter panel above the table doubles as the scrape scope, so spell it out
  // next to the button -- otherwise the link between the two is invisible.
  const scrapeFilter = parseFilter({
    country: searchParams.get("country") ?? undefined,
    gender: searchParams.get("gender") ?? undefined,
    provider: searchParams.get("provider") ?? undefined,
    age_op: searchParams.get("age_op") ?? undefined,
    age: searchParams.get("age") ?? undefined,
    joined_op: searchParams.get("joined_op") ?? undefined,
    joined_date: searchParams.get("joined_date") ?? undefined,
    active_op: searchParams.get("active_op") ?? undefined,
    active_date: searchParams.get("active_date") ?? undefined,
  });
  const scopeBits: string[] = [];
  if (scrapeFilter.countries.length) scopeBits.push(scrapeFilter.countries.join(", "));
  if (scrapeFilter.genders.length) scopeBits.push(scrapeFilter.genders.join(" / "));
  if (scrapeFilter.providers.length)
    scopeBits.push(scrapeFilter.providers.join(" / ") + " email");
  if (scrapeFilter.ageOp && scrapeFilter.ageValue) {
    const word =
      scrapeFilter.ageOp === "over"
        ? "over"
        : scrapeFilter.ageOp === "less"
          ? "under"
          : "exactly";
    scopeBits.push(`${word} ${scrapeFilter.ageValue} yrs`);
  }
  if (scrapeFilter.joinedOp && scrapeFilter.joinedDate) {
    scopeBits.push(`joined ${scrapeFilter.joinedOp} ${scrapeFilter.joinedDate}`);
  }
  if (scrapeFilter.activeOp && scrapeFilter.activeDate) {
    scopeBits.push(`active ${scrapeFilter.activeOp} ${scrapeFilter.activeDate}`);
  }
  const scopeText = scopeBits.join(" · ");

  // How many users this run should collect before stopping. The GitHub scraper
  // enumerates a world far bigger than any one run, so it is the one that needs
  // pointing at a number; the others walk a finite feed to its end.
  const supportsTarget = source === "github";
  const wanted = Number(target) || 0;

  const stopPolling = useCallback(() => {
    if (poll.current) {
      clearInterval(poll.current);
      poll.current = null;
    }
  }, []);

  const fetchStatus = useCallback(async (): Promise<JobStatus> => {
    const res = await fetch(`/api/scrape?source=${source}`, { cache: "no-store" });
    return (await res.json()) as JobStatus;
  }, [source]);

  // Load current status on mount (and adopt an in-progress run if any).
  useEffect(() => {
    let alive = true;
    fetchStatus().then((s) => {
      if (!alive) return;
      setJob(s);
      if (s.status === "running") startPolling();
    });
    return () => {
      alive = false;
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);

  const startPolling = useCallback(() => {
    stopPolling();
    poll.current = setInterval(async () => {
      const s = await fetchStatus();
      setJob(s);
      // Refresh the list every tick so new records stream in live, plus a
      // final refresh once the run ends.
      router.refresh();
      if (s.status !== "running") {
        stopPolling();
        setBusy(false);
      }
    }, 1500);
  }, [fetchStatus, router, stopPolling]);

  async function onStop() {
    setJob((j) => ({ ...j, message: "stopping…" }));
    try {
      await fetch(`/api/scrape/stop?source=${source}`, {
        method: "POST",
        cache: "no-store",
      });
    } catch {
      /* the poller will reflect the final state */
    }
  }

  async function onClick() {
    if (running) return;
    setBusy(true);
    setJob((j) => ({ ...j, status: "running", message: "" }));
    try {
      const qs = new URLSearchParams({ source });
      // How many users to collect this run. Blank = the server's default batch.
      if (supportsTarget && wanted > 0) qs.set("limit", String(wanted));
      // Target the scrape at whatever the view is filtered to: scrape only
      // those countries / account ages / gender. So "filter, then Rescrape"
      // goes and finds more of exactly what you're looking at.
      if (supportsTarget) {
        const f = parseFilter({
          country: searchParams.get("country") ?? undefined,
          gender: searchParams.get("gender") ?? undefined,
          provider: searchParams.get("provider") ?? undefined,
          age_op: searchParams.get("age_op") ?? undefined,
          age: searchParams.get("age") ?? undefined,
          joined_op: searchParams.get("joined_op") ?? undefined,
          joined_date: searchParams.get("joined_date") ?? undefined,
          active_op: searchParams.get("active_op") ?? undefined,
          active_date: searchParams.get("active_date") ?? undefined,
        });
        if (f.countries.length) qs.set("country", f.countries.join(","));
        if (f.genders.length) qs.set("gender", f.genders.join(","));
        if (f.providers.length) qs.set("provider", f.providers.join(","));
        const { min, max } = ageToRange(f);
        if (min) qs.set("age_min", min);
        if (max) qs.set("age_max", max);
        if (f.joinedOp && f.joinedDate) {
          qs.set("joined_op", f.joinedOp);
          qs.set("joined_date", f.joinedDate);
        }
        if (f.activeOp && f.activeDate) {
          qs.set("active_op", f.activeOp);
          qs.set("active_date", f.activeDate);
        }
      }
      const res = await fetch(`/api/scrape?${qs}`, {
        method: "POST",
        cache: "no-store",
      });
      const data = await res.json();
      if (!data.ok && data.error && data.error !== "already running") {
        setJob({ status: "error", message: data.error });
        setBusy(false);
        return;
      }
      startPolling();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      setJob({ status: "error", message });
      setBusy(false);
    }
  }

  let statusText = `Last scraped ${relTime(job.last_run)}`;
  if (running) {
    // Progress is counted in users collected *this run*, which is what the
    // number in the box asks for -- "+40 of 1000", not the size of the dataset.
    const added = job.added ?? 0;
    statusText =
      wanted > 0
        ? `Scraping… +${added} of ${wanted}`
        : job.added != null
          ? `Scraping… +${added} new`
          : "Scraping…";
  } else if (job.status === "error") {
    statusText = `Error: ${job.message || job.error || "failed"}`;
  } else if (job.status === "stopped") {
    statusText = `Stopped · +${job.added ?? 0} new`;
  } else if (job.status === "done" && job.added != null) {
    statusText = `+${job.added} new · ${relTime(job.last_run)}`;
  }

  return (
    <div className="rescrape">
      {supportsTarget && (
        <label
          className="scrape-target"
          title="How many users to collect this run. Blank = 50. The walk is spread across every country, so a bigger number goes wider, not deeper into one place."
        >
          <span>Users</span>
          <input
            type="number"
            min={1}
            step={50}
            inputMode="numeric"
            value={target}
            placeholder="50"
            disabled={running}
            onChange={(e) => setTarget(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !running) onClick();
            }}
          />
        </label>
      )}
      <button
        className="rescrape-btn"
        onClick={onClick}
        disabled={running}
        title={
          supportsTarget && scopeText
            ? `Scrape ${wanted > 0 ? wanted : "more"} ${label} users matching: ${scopeText}`
            : supportsTarget && wanted > 0
              ? `Scrape ${wanted} more ${label} users with an email`
              : `Re-scrape ${label} (only fetches new content)`
        }
      >
        {running ? (
          <span className="spin-mini-inline" aria-hidden="true" />
        ) : (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M21 12a9 9 0 1 1-2.64-6.36" />
            <path d="M21 3v6h-6" />
          </svg>
        )}
        {running ? "Scraping…" : `Rescrape ${label}`}
      </button>
      {supportsTarget && scopeText && !running && (
        <span
          className="scrape-scope"
          title="The Country / Gender / Age / Email filters above set what this scrape targets. Clear them to scrape everything."
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          targeting {scopeText}
        </span>
      )}
      {running && (
        <button className="stop-btn" onClick={onStop} title="Stop scraping (keeps what was collected)">
          <svg viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true">
            <rect x="6" y="6" width="12" height="12" rx="2" />
          </svg>
          Stop
        </button>
      )}
      <span className="rescrape-status" data-state={running ? "running" : job.status}>
        {statusText}
      </span>
    </div>
  );
}
