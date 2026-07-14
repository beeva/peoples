"use client";

import { useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { Column, DisplayUser } from "@/lib/slack";

const PER_PAGE = 50;

type SortDir = "asc" | "desc";
type Sort = { key: string; dir: SortDir };

/** Value used for sorting a user by a given column key. */
function sortValue(u: DisplayUser, key: string): unknown {
  if (key === "__name") return u.name.toLowerCase();
  if (key === "__freq") return u.freq;
  const v = u.fields[key];
  return typeof v === "string" ? v.toLowerCase() : v;
}

/** Empties always sort last; otherwise numeric/boolean/locale compare. */
function compareUsers(a: DisplayUser, b: DisplayUser, sort: Sort): number {
  const av = sortValue(a, sort.key);
  const bv = sortValue(b, sort.key);
  const ae = av === "" || av === null || av === undefined;
  const be = bv === "" || bv === null || bv === undefined;
  if (ae && be) return 0;
  if (ae) return 1;
  if (be) return -1;
  let r: number;
  if (typeof av === "number" && typeof bv === "number") r = av - bv;
  else if (typeof av === "boolean" && typeof bv === "boolean")
    r = av === bv ? 0 : av ? -1 : 1;
  else r = String(av).localeCompare(String(bv));
  return sort.dir === "asc" ? r : -r;
}

/** Compact page-number list with ellipses, e.g. [1, "…", 7, 8, 9, "…", 40]. */
function buildPages(cur: number, total: number): (number | "…")[] {
  const range: number[] = [];
  for (let i = Math.max(2, cur - 1); i <= Math.min(total - 1, cur + 1); i++)
    range.push(i);
  const all = [1, ...range, total].filter(
    (v, i, a) => a.indexOf(v) === i && v >= 1 && v <= total,
  );
  const out: (number | "…")[] = [];
  let prev = 0;
  for (const p of all) {
    if (prev) {
      if (p - prev === 2) out.push(prev + 1);
      else if (p - prev > 2) out.push("…");
    }
    out.push(p);
    prev = p;
  }
  return out;
}

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

function Avatar({ user, size }: { user: DisplayUser; size: "sm" | "lg" }) {
  if (user.avatar) {
    // eslint-disable-next-line @next/next/no-img-element
    return (
      <img className={`avatar ${size}`} src={user.avatar} alt="" loading="lazy" />
    );
  }
  return (
    <div className={`avatar ${size}`} style={{ background: gradient(user.name) }}>
      {(user.name.trim()[0] || "?").toUpperCase()}
    </div>
  );
}

function ServerChips({ user }: { user: DisplayUser }) {
  return (
    <span className="server-chips">
      {user.servers.map((s) => (
        <span key={s.slug} className="server-chip">
          {s.name}
        </span>
      ))}
    </span>
  );
}

function DetailPanel({
  user,
  columns,
  initialServer,
  onClose,
}: {
  user: DisplayUser;
  columns: Column[];
  initialServer?: string;
  onClose: () => void;
}) {
  const [tab, setTab] = useState(
    () =>
      user.serverData.find((s) => s.slug === initialServer)?.slug ??
      user.serverData[0].slug,
  );

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

  const active =
    user.serverData.find((s) => s.slug === tab) ?? user.serverData[0];
  const fields = columns.filter((c) => {
    const v = active.fields[c.key];
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
        aria-label={`${user.name} details`}
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
            <Avatar user={user} size="lg" />
            <div className="slack-detail-id">
              <h2>{user.name}</h2>
              {user.email ? (
                <a className="slack-detail-email" href={`mailto:${user.email}`}>
                  {user.email}
                </a>
              ) : null}
              <p className="slack-detail-freq">
                Member of {user.freq} server{user.freq === 1 ? "" : "s"}
              </p>
            </div>
          </div>

          {/* One tab per server this person belongs to. */}
          <div className="tabs slack-detail-tabs" role="tablist">
            {user.serverData.map((s) => (
              <button
                key={s.slug}
                role="tab"
                aria-selected={s.slug === active.slug}
                className={`tab${s.slug === active.slug ? " active" : ""}`}
                onClick={() => setTab(s.slug)}
              >
                {s.name}
              </button>
            ))}
          </div>

          <dl className="slack-detail-list">
            {fields.map((c) => (
              <div key={c.key} className="slack-detail-row">
                <dt>{c.label}</dt>
                <dd>
                  <Cell col={c} value={active.fields[c.key]} />
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
  users,
  view,
}: {
  columns: Column[];
  users: DisplayUser[];
  view: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<Sort>({ key: "__name", dir: "asc" });

  // The open user lives in the URL (?user=<key>) so detail views are shareable.
  const userKey = searchParams.get("user");
  const selected = userKey
    ? (users.find((u) => u.key === userKey) ?? null)
    : null;

  function open(user: DisplayUser) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("user", user.key);
    router.push(`${pathname}?${params.toString()}`, { scroll: false });
  }

  function close() {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("user");
    const qs = params.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (!term) return users;
    return users.filter((u) => {
      if (u.name.toLowerCase().includes(term)) return true;
      if (u.email.toLowerCase().includes(term)) return true;
      if (u.servers.some((s) => s.name.toLowerCase().includes(term))) return true;
      for (const c of columns) {
        const v = u.fields[c.key];
        if (v != null && String(v).toLowerCase().includes(term)) return true;
      }
      return false;
    });
  }, [q, users, columns]);

  const sorted = useMemo(
    () => [...filtered].sort((a, b) => compareUsers(a, b, sort)),
    [filtered, sort],
  );

  const totalPages = Math.max(1, Math.ceil(sorted.length / PER_PAGE));
  const current = Math.min(page, totalPages);
  const from = sorted.length ? (current - 1) * PER_PAGE : 0;
  const shown = sorted.slice(from, from + PER_PAGE);

  function toggleSort(key: string) {
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "asc" },
    );
    setPage(1);
  }

  function SortHeader({
    sortKey,
    label,
    className,
    kind,
  }: {
    sortKey: string;
    label: string;
    className?: string;
    kind?: string;
  }) {
    const active = sort.key === sortKey;
    return (
      <th
        className={`sortable${active ? " sorted" : ""}${className ? ` ${className}` : ""}`}
        data-kind={kind}
        aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
        onClick={() => toggleSort(sortKey)}
      >
        {label}
        <span className="sort-arrow">{active ? (sort.dir === "asc" ? "▲" : "▼") : ""}</span>
      </th>
    );
  }

  return (
    <>
      <div className="slack-controls">
        <input
          type="search"
          className="slack-search"
          placeholder="Search users, servers…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setPage(1);
          }}
        />
        <span className="result-meta">
          {sorted.length > 0 ? (
            <>
              Showing <b>{(from + 1).toLocaleString()}</b>–
              <b>{Math.min(from + PER_PAGE, sorted.length).toLocaleString()}</b>{" "}
              of <b>{sorted.length.toLocaleString()}</b>
              {sorted.length !== users.length
                ? ` (of ${users.length.toLocaleString()})`
                : ""}{" "}
              users
            </>
          ) : (
            "0 users"
          )}
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
                <SortHeader sortKey="__name" label="User" className="col-contact" />
                <SortHeader sortKey="__freq" label="Servers" className="col-servers" />
                {columns.map((c) => (
                  <SortHeader
                    key={c.key}
                    sortKey={c.key}
                    label={c.label}
                    kind={c.kind}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((u) => (
                <tr
                  key={u.key}
                  className="slack-row"
                  onClick={() => open(u)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      open(u);
                    }
                  }}
                >
                  <td className="col-contact">
                    <div className="who">
                      <div className="name">{u.name}</div>
                      <ServerChips user={u} />
                    </div>
                  </td>
                  <td className="col-servers">
                    <span className="freq-badge" title={u.servers.map((s) => s.name).join(", ")}>
                      {u.freq}
                    </span>
                  </td>
                  {columns.map((c) => (
                    <td key={c.key} data-kind={c.kind}>
                      <Cell col={c} value={u.fields[c.key]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {totalPages > 1 && (
        <nav className="pager" aria-label="Pagination">
          <button
            className={`page-btn${current <= 1 ? " disabled" : ""}`}
            onClick={() => setPage(current - 1)}
            disabled={current <= 1}
            aria-label="Previous page"
          >
            ‹
          </button>
          {buildPages(current, totalPages).map((p, i) =>
            p === "…" ? (
              <span key={`gap-${i}`} className="gap">
                …
              </span>
            ) : (
              <button
                key={p}
                className={`page-btn${p === current ? " active" : ""}`}
                onClick={() => setPage(p)}
                aria-current={p === current ? "page" : undefined}
              >
                {p}
              </button>
            ),
          )}
          <button
            className={`page-btn${current >= totalPages ? " disabled" : ""}`}
            onClick={() => setPage(current + 1)}
            disabled={current >= totalPages}
            aria-label="Next page"
          >
            ›
          </button>
        </nav>
      )}

      {selected && (
        <DetailPanel
          user={selected}
          columns={columns}
          initialServer={view === "all" ? undefined : view}
          onClose={close}
        />
      )}
    </>
  );
}
