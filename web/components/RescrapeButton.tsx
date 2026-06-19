"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { SourceKey } from "@/lib/emails";

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
  const [job, setJob] = useState<JobStatus>({ status: "idle" });
  const [busy, setBusy] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  const running = busy || job.status === "running";

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
      const res = await fetch(`/api/scrape?source=${source}`, {
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
    statusText = job.added != null ? `Scraping… +${job.added} new` : "Scraping…";
  } else if (job.status === "error") {
    statusText = `Error: ${job.message || job.error || "failed"}`;
  } else if (job.status === "stopped") {
    statusText = `Stopped · +${job.added ?? 0} new`;
  } else if (job.status === "done" && job.added != null) {
    statusText = `+${job.added} new · ${relTime(job.last_run)}`;
  }

  return (
    <div className="rescrape">
      <button
        className="rescrape-btn"
        onClick={onClick}
        disabled={running}
        title={`Re-scrape ${label} (only fetches new content)`}
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
