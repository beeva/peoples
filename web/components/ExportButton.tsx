"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { flagEmoji } from "@/lib/display";

/** One row of the export = one contact's MAIN email (emails[0]) plus the
 *  columns the CSV carries. Mirrors the slim rows /api/export returns. */
interface ExportRow {
  id: string;
  name: string;
  username: string;
  email: string;
  provider: string; // mailbox bucket: gmail | outlook | hotmail | personal | company
  phone: string; // E.164, "" when none was found
  whatsapp: boolean; // the number was published as a WhatsApp contact
  gender: string;
  country: string;
  run: number; // step (scrape run number)
  created_at: string; // join date
  activity_at: string;
  messaged: boolean;
}

interface ExportOptions {
  runs: { run: number; count: number }[];
  countries: { name: string; code: string; count: number }[];
  /** Mailbox buckets, in display order, with how many contacts are in each.
   *  The server owns the keys and labels so both sides can't drift. */
  providers: { key: string; label: string; count: number }[];
  /** Reachability by number: "phone" (any) and "whatsapp" (the stricter one).
   *  Same shape and same ownership rule as `providers`. */
  contactable: { key: string; label: string; count: number }[];
}

interface ExportData {
  items: ExportRow[];
  matched: number;
  options: ExportOptions;
}

type DateOp = "" | "after" | "before";

const GENDERS: { key: string; label: string }[] = [
  { key: "male", label: "Male" },
  { key: "female", label: "Female" },
  { key: "unknown", label: "Unknown" },
];

const STATUSES: { key: "sent" | "unsent"; label: string }[] = [
  { key: "sent", label: "Sent" },
  { key: "unsent", label: "Unsent" },
];

function formatDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** RFC-4180 quoting: wrap in quotes, double any quotes inside. */
function csvCell(v: string | number): string {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function buildCsv(rows: ExportRow[], providerLabel: (key: string) => string): string {
  const header = [
    "Name", "Username", "Email", "Provider", "Phone", "WhatsApp",
    "Gender", "Country", "Step", "Sent", "Joined", "Last Active",
  ];
  const lines = [header.join(",")];
  for (const r of rows) {
    lines.push(
      [
        r.name, r.username, r.email, providerLabel(r.provider),
        // Leading apostrophe: spreadsheets otherwise read "+4477..." as a
        // formula and mangle the number.
        r.phone ? `'${r.phone}` : "", r.phone ? (r.whatsapp ? "yes" : "no") : "",
        r.gender, r.country,
        r.run || "", r.messaged ? "yes" : "no", r.created_at, r.activity_at,
      ]
        .map(csvCell)
        .join(","),
    );
  }
  return lines.join("\r\n") + "\r\n";
}

export default function ExportButton({
  source,
  label,
}: {
  source: string;
  label: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [marking, setMarking] = useState(false);
  // Arms the "mark as sent" button. The first click asks, the second acts --
  // window.confirm() is suppressible ("prevent additional dialogs"), and when
  // it is suppressed it returns false, so the button silently did nothing.
  const [confirmMark, setConfirmMark] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState<ExportData | null>(null);

  // Filter state. Empty selections mean "everything" -- same convention as
  // the facet filters above the main table.
  const [count, setCount] = useState(""); // "" = export all matched
  const [activeOp, setActiveOp] = useState<DateOp>("");
  const [activeDate, setActiveDate] = useState("");
  const [joinedOp, setJoinedOp] = useState<DateOp>("");
  const [joinedDate, setJoinedDate] = useState("");
  const [statuses, setStatuses] = useState<Set<string>>(new Set());
  const [genders, setGenders] = useState<Set<string>>(new Set());
  const [providers, setProviders] = useState<Set<string>>(new Set());
  const [reach, setReach] = useState<Set<string>>(new Set());
  const [runs, setRuns] = useState<Set<number>>(new Set());
  const [countries, setCountries] = useState<Set<string>>(new Set());
  const [countryQuery, setCountryQuery] = useState("");

  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Bumped after "mark as sent" so the preview re-fetches fresh statuses.
  const [refresh, setRefresh] = useState(0);

  function openModal() {
    setError("");
    setOpen(true);
  }

  // Never leave the button armed across a change of what it would act on:
  // closing the dialog or a fresh preview both disarm it.
  useEffect(() => {
    setConfirmMark(false);
  }, [open, data]);

  // Re-fetch the preview whenever the dialog is open and any condition
  // changes (debounced so typing a count doesn't fire per keystroke).
  useEffect(() => {
    if (!open) return;
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(async () => {
      setLoading(true);
      setError("");
      try {
        const qs = new URLSearchParams({ source });
        const n = Number(count);
        if (Number.isFinite(n) && n > 0) qs.set("limit", String(Math.floor(n)));
        if (activeOp && activeDate) {
          qs.set("active_op", activeOp);
          qs.set("active_date", activeDate);
        }
        if (joinedOp && joinedDate) {
          qs.set("joined_op", joinedOp);
          qs.set("joined_date", joinedDate);
        }
        if (statuses.size) qs.set("messaged", [...statuses].join(","));
        if (genders.size) qs.set("gender", [...genders].join(","));
        if (providers.size) qs.set("provider", [...providers].join(","));
        if (reach.size) qs.set("contactable", [...reach].join(","));
        if (runs.size) qs.set("runs", [...runs].join(","));
        if (countries.size) qs.set("country", [...countries].join(","));
        const res = await fetch(`/api/export?${qs}`, { cache: "no-store" });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.error || `Server responded ${res.status}`);
        }
        setData((await res.json()) as ExportData);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setLoading(false);
      }
    }, 250);
    return () => {
      if (debounce.current) clearTimeout(debounce.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, source, count, activeOp, activeDate, joinedOp, joinedDate,
      statuses, genders, providers, reach, runs, countries, refresh]);

  // Close on Escape, like the detail drawer.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function toggle<T>(set: Set<T>, value: T, apply: (next: Set<T>) => void) {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    apply(next);
  }

  const visibleCountries = useMemo(() => {
    const all = data?.options.countries ?? [];
    const q = countryQuery.trim().toLowerCase();
    if (!q) return all;
    // Keep selected entries visible even when they don't match the search,
    // so a choice can always be un-made.
    return all.filter(
      (c) => c.name.toLowerCase().includes(q) || countries.has(c.name),
    );
  }, [data, countryQuery, countries]);

  /** Bucket key -> human label, straight from the server's option list. */
  const providerLabel = useMemo(() => {
    const byKey = new Map(
      (data?.options.providers ?? []).map((p) => [p.key, p.label]),
    );
    return (key: string) => byKey.get(key) || key || "";
  }, [data]);

  function download() {
    const rows = data?.items ?? [];
    if (!rows.length) return;
    // BOM so Excel opens the file as UTF-8 (names carry accents/CJK).
    const blob = new Blob(["﻿" + buildCsv(rows, providerLabel)], {
      type: "text/csv;charset=utf-8",
    });
    const stamp = new Date().toISOString().slice(0, 10);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${source}-emails-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    window.dispatchEvent(
      new CustomEvent("toast", { detail: `Exported ${rows.length} emails to CSV` }),
    );
  }

  /** Flag every previewed contact as sent (manual, like the row checkbox). */
  async function markAllSent() {
    const rows = data?.items ?? [];
    if (!rows.length || marking) return;
    if (!confirmMark) {
      setConfirmMark(true); // first click arms; the label asks for confirmation
      return;
    }
    setConfirmMark(false);
    setMarking(true);
    setError("");
    try {
      const res = await fetch("/api/message/mark", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ids: rows.map((r) => r.id), sent: true }),
      });
      const body = await res.json();
      if (!body.ok) throw new Error(body.error || "mark failed");
      window.dispatchEvent(
        new CustomEvent("toast", { detail: `Marked ${body.marked} contacts as sent` }),
      );
      setRefresh((n) => n + 1); // preview statuses changed -- re-fetch
      router.refresh(); // and the main table's Sent badges too
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setMarking(false);
    }
  }

  const rows = data?.items ?? [];
  const matched = data?.matched ?? 0;

  return (
    <>
      <button
        className="export-btn"
        onClick={openModal}
        title={`Export ${label} users' main emails as a CSV file`}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <path d="M7 10l5 5 5-5" />
          <path d="M12 15V3" />
        </svg>
        Export CSV
      </button>

      {open && (
        <div
          className="modal-overlay"
          onClick={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div
            className="modal modal-wide"
            role="dialog"
            aria-modal="true"
            aria-label={`Export ${label} emails`}
          >
            <div className="modal-head">
              <h2>Export {label} emails</h2>
              <button className="icon-btn" onClick={() => setOpen(false)} aria-label="Close">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
                     strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="modal-body">
              <div className="export-conditions">
                <div className="export-cond">
                  <span className="facet-label">Count</span>
                  <input
                    className="export-count"
                    type="number"
                    min={1}
                    step={50}
                    inputMode="numeric"
                    placeholder="all"
                    value={count}
                    onChange={(e) => setCount(e.target.value)}
                    aria-label="How many contacts to export"
                    title="How many contacts to export (newest activity first). Blank = all matched."
                  />
                </div>

                <div className="export-cond">
                  <span className="facet-label">Last active</span>
                  <select
                    className="facet-op"
                    value={activeOp}
                    onChange={(e) => setActiveOp(e.target.value as DateOp)}
                    aria-label="Last-active comparison"
                  >
                    <option value="">Any time</option>
                    <option value="after">After</option>
                    <option value="before">Before</option>
                  </select>
                  <input
                    className="export-date"
                    type="date"
                    value={activeDate}
                    disabled={activeOp === ""}
                    onChange={(e) => setActiveDate(e.target.value)}
                    aria-label="Last-active date"
                  />
                </div>

                <div className="export-cond">
                  <span className="facet-label">Joined</span>
                  <select
                    className="facet-op"
                    value={joinedOp}
                    onChange={(e) => setJoinedOp(e.target.value as DateOp)}
                    aria-label="Join-date comparison"
                  >
                    <option value="">Any time</option>
                    <option value="after">After</option>
                    <option value="before">Before</option>
                  </select>
                  <input
                    className="export-date"
                    type="date"
                    value={joinedDate}
                    disabled={joinedOp === ""}
                    onChange={(e) => setJoinedDate(e.target.value)}
                    aria-label="Join date"
                  />
                </div>

                <div className="export-cond">
                  <span className="facet-label">Status</span>
                  {STATUSES.map((s) => (
                    <label
                      key={s.key}
                      className={`check-pill${statuses.has(s.key) ? " on" : ""}`}
                    >
                      <input
                        type="checkbox"
                        checked={statuses.has(s.key)}
                        onChange={() => toggle(statuses, s.key, setStatuses)}
                      />
                      <span>{s.label}</span>
                    </label>
                  ))}
                </div>

                <div className="export-cond">
                  <span className="facet-label">Gender</span>
                  {GENDERS.map((g) => (
                    <label
                      key={g.key}
                      className={`check-pill${genders.has(g.key) ? " on" : ""}`}
                    >
                      <input
                        type="checkbox"
                        checked={genders.has(g.key)}
                        onChange={() => toggle(genders, g.key, setGenders)}
                      />
                      <span>{g.label}</span>
                    </label>
                  ))}
                </div>

                <div className="export-cond">
                  <span className="facet-label">Email</span>
                  {(data?.options.providers ?? []).map((p) => (
                    <label
                      key={p.key}
                      className={`check-pill${providers.has(p.key) ? " on" : ""}`}
                      title={`Contacts whose main email is a ${p.label} address`}
                    >
                      <input
                        type="checkbox"
                        checked={providers.has(p.key)}
                        onChange={() => toggle(providers, p.key, setProviders)}
                      />
                      <span>{p.label}</span>
                      <span className="check-count">{p.count.toLocaleString()}</span>
                    </label>
                  ))}
                  {!data && <span className="facet-empty">Loading…</span>}
                </div>

                <div className="export-cond">
                  <span className="facet-label">Reach</span>
                  {(data?.options.contactable ?? []).map((r) => (
                    <label
                      key={r.key}
                      className={`check-pill${reach.has(r.key) ? " on" : ""}`}
                      title={
                        r.key === "whatsapp"
                          ? "Contacts whose number was published as a WhatsApp contact"
                          : "Contacts with any phone number"
                      }
                    >
                      <input
                        type="checkbox"
                        checked={reach.has(r.key)}
                        onChange={() => toggle(reach, r.key, setReach)}
                      />
                      <span>{r.label}</span>
                      <span className="check-count">{r.count.toLocaleString()}</span>
                    </label>
                  ))}
                  {!data && <span className="facet-empty">Loading…</span>}
                </div>

                <div className="export-cond">
                  <span className="facet-label">Step</span>
                  {(data?.options.runs ?? []).map((r) => (
                    <label
                      key={r.run}
                      className={`check-pill${runs.has(r.run) ? " on" : ""}`}
                    >
                      <input
                        type="checkbox"
                        checked={runs.has(r.run)}
                        onChange={() => toggle(runs, r.run, setRuns)}
                      />
                      <span>Run {r.run}</span>
                      <span className="check-count">{r.count.toLocaleString()}</span>
                    </label>
                  ))}
                  {data && data.options.runs.length === 0 && (
                    <span className="facet-empty">No runs yet</span>
                  )}
                </div>

                <div className="export-cond export-cond-positions">
                  <span className="facet-label">
                    Country{countries.size > 0 ? ` · ${countries.size}` : ""}
                  </span>
                  <input
                    className="export-pos-search"
                    type="search"
                    placeholder="Filter countries…"
                    value={countryQuery}
                    onChange={(e) => setCountryQuery(e.target.value)}
                    aria-label="Search countries"
                  />
                  <div className="export-pos-scroll">
                    {visibleCountries.length === 0 && (
                      <span className="facet-empty">
                        {data ? "No countries" : "Loading…"}
                      </span>
                    )}
                    {visibleCountries.map((c) => (
                      <label
                        key={c.name}
                        className={`check-pill${countries.has(c.name) ? " on" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={countries.has(c.name)}
                          onChange={() => toggle(countries, c.name, setCountries)}
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

              {error && (
                <div className="msg-status" data-state="error">
                  Error: {error}
                </div>
              )}

              <div className="export-preview" aria-busy={loading}>
                <table>
                  <thead>
                    <tr>
                      <th className="col-num">#</th>
                      <th>Name</th>
                      <th>Username</th>
                      <th>Email</th>
                      <th>Provider</th>
                      <th>Phone</th>
                      <th>Gender</th>
                      <th>Country</th>
                      <th>Step</th>
                      <th>Status</th>
                      <th>Joined</th>
                      <th>Last active</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr key={r.id}>
                        <td className="col-num">{i + 1}</td>
                        <td>{r.name || "—"}</td>
                        <td>{r.username || "—"}</td>
                        <td className="export-email">{r.email}</td>
                        <td>{providerLabel(r.provider) || "—"}</td>
                        <td>
                          {r.phone ? (
                            <span className={`phone-chip${r.whatsapp ? " wa" : ""}`}>
                              {r.whatsapp ? "WhatsApp " : ""}
                              {r.phone}
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td>{r.gender || "—"}</td>
                        <td>{r.country || "—"}</td>
                        <td className="col-num">{r.run || "—"}</td>
                        <td className={r.messaged ? "export-sent" : ""}>
                          {r.messaged ? "sent" : "—"}
                        </td>
                        <td>{formatDate(r.created_at)}</td>
                        <td>{formatDate(r.activity_at)}</td>
                      </tr>
                    ))}
                    {rows.length === 0 && (
                      <tr>
                        <td colSpan={11} className="export-none">
                          {loading || !data
                            ? "Loading…"
                            : "No contacts match these conditions."}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="modal-foot">
              <span className="export-meta">
                {loading || !data
                  ? "Updating preview…"
                  : `${rows.length.toLocaleString()} of ${matched.toLocaleString()} matching will be exported`}
              </span>
              <button className="btn-secondary" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                className={`btn-secondary${confirmMark ? " is-confirming" : ""}`}
                onClick={markAllSent}
                disabled={marking || loading || rows.length === 0}
                title="Flag every contact in the preview as sent (same as ticking each row's Sent box)"
              >
                {marking
                  ? "Marking…"
                  : confirmMark
                    ? `Click again to mark ${rows.length.toLocaleString()}`
                    : `✓ Mark ${rows.length.toLocaleString()} as sent`}
              </button>
              <button
                className="btn-primary"
                onClick={download}
                disabled={loading || rows.length === 0}
              >
                Download CSV ({rows.length.toLocaleString()})
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
