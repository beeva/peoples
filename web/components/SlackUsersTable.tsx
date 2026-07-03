"use client";

import { useEffect, useMemo, useState } from "react";
import type { Column } from "@/lib/slack";

const PAGE = 100;

const AVATAR_COLORS: [string, string][] = [
  ["#6ea8fe", "#3b6fd4"],
  ["#b794f6", "#7c54c9"],
  ["#4ade80", "#22a35a"],
  ["#f472b6", "#c43d82"],
  ["#fbbf24", "#d18d09"],
  ["#38bdf8", "#0a82bd"],
];

function gradient(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const [a, b] = AVATAR_COLORS[h % AVATAR_COLORS.length];
  return `linear-gradient(135deg, ${a}, ${b})`;
}

function formatDate(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n) || n <= 0) return "";
  // Slack timestamps are seconds; anything smaller than ~year 2001 in ms is odd.
  const d = new Date(n < 1e12 ? n * 1000 : n);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function Cell({ col, value }: { col: Column; value: unknown }) {
  if (value === "" || value === null || value === undefined) {
    return <span className="muted">—</span>;
  }
  switch (col.kind) {
    case "bool":
      return value ? (
        <span className="sent-badge">✓</span>
      ) : (
        <span className="muted">—</span>
      );
    case "email":
      return <a href={`mailto:${value}`}>{String(value)}</a>;
    case "date": {
      const d = formatDate(value);
      return d ? <>{d}</> : <span className="muted">—</span>;
    }
    case "id":
      return <code className="slack-id">{String(value)}</code>;
    default:
      return <>{String(value)}</>;
  }
}

type Row = Record<string, unknown>;

function Avatar({ row, size }: { row: Row; size: "sm" | "lg" }) {
  const name = String(row.__name || "?");
  const avatar = String(row.__avatar || "");
  if (avatar) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img className={`avatar ${size}`} src={avatar} alt="" loading="lazy" />;
  }
  return (
    <div className={`avatar ${size}`} style={{ background: gradient(name) }}>
      {(name.trim()[0] || "?").toUpperCase()}
    </div>
  );
}

function DetailPanel({
  row,
  columns,
  onClose,
}: {
  row: Row;
  columns: Column[];
  onClose: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  const name = String(row.__name || "Unknown");
  const email = row["profile.email"];
  const title = row["profile.title"];
  const fields = columns.filter((c) => {
    const v = row[c.key];
    return v !== "" && v !== null && v !== undefined;
  });

  return (
    <div
      className="drawer-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`${name} details`}
      >
        <div className="drawer-bar">
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
            Close
          </button>
        </div>
        <div className="drawer-body">
          <div className="slack-detail-head">
            <Avatar row={row} size="lg" />
            <div className="slack-detail-id">
              <h2>{name}</h2>
              {title ? <p className="slack-detail-title">{String(title)}</p> : null}
              {email ? (
                <a className="slack-detail-email" href={`mailto:${email}`}>
                  {String(email)}
                </a>
              ) : null}
            </div>
          </div>

          <dl className="slack-detail-list">
            {fields.map((c) => (
              <div key={c.key} className="slack-detail-row">
                <dt>{c.label}</dt>
                <dd>
                  <Cell col={c} value={row[c.key]} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </aside>
    </div>
  );
}

export default function SlackUsersTable({
  columns,
  rows,
}: {
  columns: Column[];
  rows: Row[];
}) {
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const [selected, setSelected] = useState<Row | null>(null);

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (!term) return rows;
    return rows.filter((r) => {
      if (String(r.__name).toLowerCase().includes(term)) return true;
      for (const c of columns) {
        const v = r[c.key];
        if (v != null && String(v).toLowerCase().includes(term)) return true;
      }
      return false;
    });
  }, [q, rows, columns]);

  const shown = filtered.slice(0, limit);

  return (
    <>
      <div className="slack-controls">
        <input
          type="search"
          className="slack-search"
          placeholder="Search users…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setLimit(PAGE);
          }}
        />
        <span className="result-meta">
          {filtered.length.toLocaleString()}
          {filtered.length !== rows.length
            ? ` of ${rows.length.toLocaleString()}`
            : ""}{" "}
          users
        </span>
      </div>

      {shown.length === 0 ? (
        <div className="empty">
          <div>No users match “{q}”.</div>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="contact-table slack-table">
            <thead>
              <tr>
                <th className="col-contact">User</th>
                {columns.map((c) => (
                  <th key={c.key} data-kind={c.kind}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => (
                <tr
                  key={(r.id as string) ?? i}
                  className="slack-row"
                  onClick={() => setSelected(r)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelected(r);
                    }
                  }}
                >
                  <td className="col-contact">
                    <div className="who">
                      <div className="name">{String(r.__name)}</div>
                    </div>
                  </td>
                  {columns.map((c) => (
                    <td key={c.key} data-kind={c.kind}>
                      <Cell col={c} value={r[c.key]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {limit < filtered.length && (
        <div className="slack-more">
          <button className="tab" onClick={() => setLimit((n) => n + PAGE)}>
            Show more ({(filtered.length - limit).toLocaleString()} remaining)
          </button>
        </div>
      )}

      {selected && (
        <DetailPanel
          row={selected}
          columns={columns}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}
