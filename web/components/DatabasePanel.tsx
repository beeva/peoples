"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Database status and SQL export/import.
 *
 *  `Export` dumps the whole database to a .sql file and `Import` restores one
 *  over the top. There is no "sync" step in the normal case: the scrapers write
 *  into these tables as they collect, so what is shown here is already current.
 *  The import-legacy-files control only appears when a data file from before
 *  the database is still sitting on disk.
 */

interface SourceState {
  source: string;
  label: string;
  records: number;
  contacts: number;
  runs: number;
  listed: boolean;
  /** A pre-database data file still on disk, if any. */
  legacy_file: string;
  legacy_pending: boolean;
}

interface Backup {
  filename: string;
  path: string;
  bytes: number;
  created_at: string;
}

export interface DbStatus {
  connected: boolean;
  host?: string;
  port?: number;
  user?: string;
  database?: string;
  server?: string;
  size_bytes?: number;
  tables?: Record<string, number>;
  sources?: SourceState[];
  backup_dir?: string;
  backups?: Backup[];
  error?: string;
}

function human(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

function when(iso: string): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function toast(message: string) {
  window.dispatchEvent(new CustomEvent("toast", { detail: message }));
}

const TABLE_NOTES: Record<string, string> = {
  records: "one row per scraped occurrence, with its original JSON",
  contacts: "merged people — what the list shows",
  contact_emails: "every address, for the sent-log join",
  skipped: "logins ruled out, so a rescrape skips them",
  slack_users: "Slack workspace exports",
  enrichment: "Claude-inferred country + gender",
  sent_log: "who has been emailed",
  app_state: "per-source scraper state and cursors",
  sync_meta: "what a legacy file import last read",
};

export default function DatabasePanel({ initial }: { initial: DbStatus }) {
  const [status, setStatus] = useState<DbStatus>(initial);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/db", { cache: "no-store" });
      setStatus(await res.json());
    } catch {
      /* leave the last good status on screen */
    }
  }, []);

  // A restore changes what every other page is showing, so re-read the status
  // rather than trusting what is on screen.
  const invalidate = useCallback(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!status.connected) {
      const t = setTimeout(() => void refresh(), 3000);
      return () => clearTimeout(t);
    }
  }, [status.connected, refresh]);

  async function runSync(force: boolean) {
    setBusy(force ? "rebuild" : "sync");
    setError("");
    setNote("");
    try {
      const res = await fetch(`/api/db?force=${force ? 1 : 0}`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || "sync failed");
      const changed: string[] = data.changed || [];
      const msg = changed.length
        ? `Imported ${changed.join(", ")}`
        : "Nothing left to import";
      setNote(msg);
      toast(msg);
      if (data.db) setStatus(data.db);
      else invalidate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "sync failed");
    } finally {
      setBusy("");
    }
  }

  function exportSql() {
    // A plain navigation, so the browser owns the download (and its progress)
    // rather than us buffering tens of megabytes into a blob first.
    setNote("Preparing the dump — the download starts when it is written.");
    window.location.href = "/api/db/export?download=1";
  }

  async function importFrom(body: BodyInit, headers: HeadersInit, label: string) {
    setBusy("import");
    setError("");
    setNote("");
    try {
      const res = await fetch("/api/db/import", { method: "POST", body, headers });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || "import failed");
      const msg = `Restored ${label}`;
      setNote(msg);
      toast(msg);
      if (data.db) setStatus(data.db);
      else invalidate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "import failed");
    } finally {
      setBusy("");
    }
  }

  async function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // let the same file be picked again after a failure
    if (!file) return;
    if (
      !confirm(
        `Restore "${file.name}" into ${status.database}?\n\n` +
          "This replaces everything currently in the database. " +
          "Export a dump first if you might want to come back to it.",
      )
    ) {
      return;
    }
    await importFrom(
      await file.arrayBuffer(),
      { "Content-Type": "application/sql" },
      file.name,
    );
  }

  async function restoreBackup(b: Backup) {
    if (
      !confirm(
        `Restore "${b.filename}" (${human(b.bytes)})?\n\n` +
          "This replaces everything currently in the database.",
      )
    ) {
      return;
    }
    await importFrom(
      JSON.stringify({ path: b.path }),
      { "Content-Type": "application/json" },
      b.filename,
    );
  }

  const tables = status.tables || {};
  const sources = status.sources || [];
  const backups = status.backups || [];
  const anyLegacy = sources.some((s) => s.legacy_pending);

  if (!status.connected) {
    return (
      <div className="banner">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
             strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 9v4M12 17h.01" />
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
        </svg>
        <div>
          <strong>Cannot reach MySQL.</strong>{" "}
          {status.error || "The data server could not connect to the database."}{" "}
          Start it with <code>npm run db:start</code>, or run everything together
          with <code>npm run dev</code>.
        </div>
      </div>
    );
  }

  return (
    <div className="db-page">
      {error && (
        <div className="banner">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
               strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
          <div>{error}</div>
        </div>
      )}

      <section className="db-card">
        <div className="db-card-head">
          <h2>Server</h2>
          <span className="db-pill ok">connected</span>
        </div>
        <dl className="db-facts">
          <div>
            <dt>Address</dt>
            <dd>
              <code>
                {status.user}@{status.host}:{status.port}/{status.database}
              </code>
            </dd>
          </div>
          <div>
            <dt>Engine</dt>
            <dd>MySQL {status.server}</dd>
          </div>
          <div>
            <dt>On disk</dt>
            <dd>{human(status.size_bytes || 0)}</dd>
          </div>
        </dl>
      </section>

      <section className="db-card">
        <div className="db-card-head">
          <h2>Tables</h2>
        </div>
        <table className="db-table">
          <tbody>
            {Object.entries(tables).map(([name, n]) => (
              <tr key={name}>
                <th scope="row">
                  <code>{name}</code>
                </th>
                <td className="db-num">{n.toLocaleString()}</td>
                <td className="db-note">{TABLE_NOTES[name] || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="db-card">
        <div className="db-card-head">
          <h2>Sources</h2>
        </div>
        <p className="db-hint">
          Each scraper writes straight into these tables as it collects, so a
          run shows up here while it is still going. <code>Records</code> is
          every occurrence scraped; <code>Contacts</code> is what the list
          shows, after occurrences sharing an email address are merged into one
          person.
        </p>
        <table className="db-table">
          <thead>
            <tr>
              <th>Source</th>
              <th className="db-num">Records</th>
              <th className="db-num">Contacts</th>
              <th className="db-num">Steps</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.source}>
                <th scope="row">{s.label}</th>
                <td className="db-num">{s.records.toLocaleString()}</td>
                <td className="db-num">{s.contacts.toLocaleString()}</td>
                <td className="db-num">{s.runs || "—"}</td>
                <td>
                  {!s.listed && (
                    <span className="db-note">stored, not a tab</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {anyLegacy && (
          <>
            <p className="db-hint db-inline-note">
              Data files from before the database are still on disk. They are
              not read while serving — import them once and they can be deleted.
            </p>
            <div className="db-actions">
              <button
                className="btn-secondary"
                onClick={() => void runSync(false)}
                disabled={!!busy}
              >
                {busy === "sync" ? "Importing…" : "Import legacy files"}
              </button>
            </div>
          </>
        )}
      </section>

      <section className="db-card">
        <div className="db-card-head">
          <h2>SQL file</h2>
        </div>
        <p className="db-hint">
          An export is an ordinary <code>mysqldump</code> — restore it here, with{" "}
          <code>npm run db:import</code>, in phpMyAdmin, or on another MySQL
          server.
        </p>
        <div className="db-actions">
          <button className="btn-primary" onClick={exportSql} disabled={!!busy}>
            Export to .sql
          </button>
          <button
            className="btn-secondary"
            onClick={() => fileInput.current?.click()}
            disabled={!!busy}
          >
            {busy === "import" ? "Restoring…" : "Import a .sql file…"}
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".sql,application/sql,text/plain"
            hidden
            onChange={(e) => void onPickFile(e)}
          />
        </div>
        {note && <p className="db-note db-inline-note">{note}</p>}

        {backups.length > 0 && (
          <>
            <h3 className="db-subhead">
              Dumps in <code>{status.backup_dir}</code>
            </h3>
            <table className="db-table">
              <tbody>
                {backups.map((b) => (
                  <tr key={b.path}>
                    <th scope="row">
                      <code>{b.filename}</code>
                    </th>
                    <td className="db-num">{human(b.bytes)}</td>
                    <td className="db-note">{when(b.created_at)}</td>
                    <td>
                      <button
                        className="btn-secondary db-small"
                        onClick={() => void restoreBackup(b)}
                        disabled={!!busy}
                      >
                        Restore
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
    </div>
  );
}
