import Link from "next/link";
import { DEFAULT_SORT, type EmailRecord, type SortKey, type SourceKey } from "@/lib/emails";
import { highlight } from "@/lib/highlight";
import {
  accountAge,
  countryDisplay,
  genderDisplay,
  joinedDisplay,
  joinedLabel,
  sentLabel,
} from "@/lib/display";
import CopyButton from "./CopyButton";
import MessageButton from "./MessageButton";
import SentToggle from "./SentToggle";

const AVATAR_COLORS: [string, string][] = [
  ["#6ea8fe", "#3b6fd4"],
  ["#b794f6", "#7c54c9"],
  ["#4ade80", "#22a35a"],
  ["#f472b6", "#c43d82"],
  ["#fbbf24", "#d18d09"],
  ["#38bdf8", "#0a82bd"],
];

const SOURCE_LABELS: Record<string, string> = {
  discourse: "three.js",
  aboutme: "about.me",
  github: "GitHub",
};

function avatarGradient(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const [a, b] = AVATAR_COLORS[h % AVATAR_COLORS.length];
  return `linear-gradient(135deg, ${a}, ${b})`;
}

function formatDate(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function Row({
  record,
  query,
  num,
}: {
  record: EmailRecord;
  query: string;
  num: number;
}) {
  const display = record.name || record.username || "Unknown";
  const initial = (display.trim()[0] || "?").toUpperCase();
  const src = record.source as SourceKey;
  // Prefer "last active" when the source tracks it (GitHub); fall back to the
  // record's own date (post date / join date) otherwise.
  const activeDate = formatDate(record.activityAt);
  const date = activeDate || formatDate(record.createdAt);
  const dateTitle = activeDate ? `Last active ${activeDate}` : date;
  // How long they have been on GitHub, under the activity date. Only GitHub's
  // `createdAt` is a join date -- elsewhere it is the date of the post itself,
  // and the row already shows that as its date.
  const isGithub = src === "github";
  const joined = isGithub ? joinedDisplay(record.createdAt) : "";
  const age = isGithub ? accountAge(record.createdAt) : "";
  const href = `/contact/${encodeURIComponent(record.id)}`;
  const primaryEmail = record.emails[0] || "";
  const extraEmails = record.emails.length - 1;
  const country = countryDisplay(record.country, record.countryCode);
  const gender = genderDisplay(record.gender);

  return (
    <tr className={record.messaged ? "messaged" : undefined}>
      <td className="col-num">{num}</td>

      <td className="col-run">
        {record.run > 0 ? (
          <span className="run-badge" title={`Collected on scrape run ${record.run}`}>
            run {record.run}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-check">
        <SentToggle id={record.id} name={display} sent={record.messaged} />
      </td>

      <td className="col-contact">
        <Link href={href} className="row-contact">
          <div className="avatar sm" style={{ background: avatarGradient(display) }}>
            {initial}
          </div>
          <div className="who">
            <div className="name">{highlight(display, query)}</div>
            {record.title ? (
              <div className="position" title={record.title}>
                {highlight(record.title, query)}
              </div>
            ) : (
              <div className="handle">
                {record.username ? `@${record.username}` : SOURCE_LABELS[src] ?? src}
              </div>
            )}
          </div>
        </Link>
      </td>

      <td className="col-gender">
        {gender ? (
          <span className="gender-badge" data-gender={record.gender}>
            {gender}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-source">
        <span className="src-badge" data-source={src}>
          {SOURCE_LABELS[src] ?? src}
        </span>
      </td>

      <td className="col-email">
        {primaryEmail ? (
          <span className="email-chip">
            <a href={`mailto:${primaryEmail}`}>{highlight(primaryEmail, query)}</a>
            <CopyButton value={primaryEmail} />
            {extraEmails > 0 && (
              <span className="more" title={record.emails.slice(1).join(", ")}>
                +{extraEmails}
              </span>
            )}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-meta">
        {record.organization && (
          <span className="meta-bit">🏢 {highlight(record.organization, query)}</span>
        )}
        {country && <span className="meta-bit">{country}</span>}
        {record.location && (
          <span className="meta-bit">📍 {highlight(record.location, query)}</span>
        )}
        {(record.url || record.siteUrl) && (
          <span className="meta-links">
            {record.url && (
              <a
                className="meta-link"
                href={record.url}
                target="_blank"
                rel="noopener noreferrer"
                title={record.url}
              >
                {SOURCE_LABELS[src] === "GitHub" ? "GitHub" : "Profile"}
              </a>
            )}
            {record.siteUrl && (
              <a
                className="meta-link"
                href={record.siteUrl}
                target="_blank"
                rel="noopener noreferrer"
                title={record.siteUrl}
              >
                Portfolio
              </a>
            )}
          </span>
        )}
        {!record.organization && !country && !record.location &&
          !record.url && !record.siteUrl && <span className="muted">—</span>}
      </td>

      <td className="col-summary">
        {record.preview ? (
          <span className="summary-text">{highlight(record.preview, query)}</span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-date">
        {date ? (
          <span title={dateTitle}>
            {activeDate && <span className="date-tag">active</span>}
            {date}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
        {joined && (
          <span className="date-joined" title={joinedLabel(record.createdAt)}>
            Joined {joined}
            {age && <span className="age-badge">{age}</span>}
          </span>
        )}
      </td>

      <td className="col-status">
        {record.messaged ? (
          <span
            className="sent-badge"
            data-manual={record.messagedManual || undefined}
            title={sentLabel(record.messagedAt, record.messagedCount, record.messagedManual)}
          >
            ✓ Sent
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-actions">
        <div className="actions">
          {primaryEmail && (
            <MessageButton id={record.id} to={primaryEmail} name={display} />
          )}
          <Link
            href={href}
            className="icon-link"
            title={`View ${display}'s details`}
            aria-label={`View ${display}'s details`}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </Link>
        </div>
      </td>
    </tr>
  );
}

/** A column header you can click to sort by. Clicking the active one flips it.
 *
 *  Rendered as a plain link so it works without JavaScript and so the sort ends
 *  up in the URL -- a sorted view can be linked, bookmarked and reloaded.
 */
function SortHeader({
  className,
  label,
  title,
  asc,
  desc,
  sort,
  baseParams,
}: {
  className: string;
  label: string;
  title?: string;
  asc: SortKey;
  desc: SortKey;
  sort: SortKey;
  baseParams: Record<string, string>;
}) {
  const active = sort === asc || sort === desc;
  // Clicking an inactive column sorts it the way you almost always want first:
  // A-Z for text, but newest/highest first for dates and run numbers.
  const next: SortKey = active ? (sort === asc ? desc : asc) : desc === "newest" || desc === "run_desc" ? desc : asc;
  const params = new URLSearchParams(baseParams);
  if (next === DEFAULT_SORT) params.delete("sort");
  else params.set("sort", next);
  params.delete("page"); // a new order means the old page number is meaningless
  const qs = params.toString();

  return (
    <th className={className} aria-sort={active ? (sort === asc ? "ascending" : "descending") : "none"}>
      <Link href={qs ? `/?${qs}` : "/"} className="col-sort" title={title || `Sort by ${label}`}>
        {label}
        <span className="sort-caret" aria-hidden="true">
          {active ? (sort === asc ? "▲" : "▼") : "↕"}
        </span>
      </Link>
    </th>
  );
}

export default function EmailTable({
  items,
  query,
  startIndex = 0,
  sort = DEFAULT_SORT,
  baseParams = {},
}: {
  items: EmailRecord[];
  query: string;
  /** How many rows precede this page, so the numbering runs 1..N across the
   *  whole result set instead of restarting at 1 on every page. */
  startIndex?: number;
  /** The order currently applied, so the header can show and flip it. */
  sort?: SortKey;
  /** The other query params (source, search, filter) to preserve in sort links. */
  baseParams?: Record<string, string>;
}) {
  const head = { sort, baseParams };
  return (
    <div className="table-wrap">
      <table className="contact-table">
        <thead>
          <tr>
            <th className="col-num">#</th>
            <SortHeader
              className="col-run"
              label="Step"
              title="Which scrape run collected this contact"
              asc="run_asc"
              desc="run_desc"
              {...head}
            />
            <th className="col-check">
              <span className="sr-only">Sent</span>
            </th>
            <SortHeader
              className="col-contact"
              label="Contact"
              asc="name_asc"
              desc="name_desc"
              {...head}
            />
            <th className="col-gender">Gender</th>
            <th className="col-source">Source</th>
            <SortHeader
              className="col-email"
              label="Email"
              asc="email_asc"
              desc="email_desc"
              {...head}
            />
            <SortHeader
              className="col-meta"
              label="Details"
              title="Sort by country"
              asc="country_asc"
              desc="country_desc"
              {...head}
            />
            <th className="col-summary">Summary</th>
            <SortHeader
              className="col-date"
              label="Date"
              asc="oldest"
              desc="newest"
              {...head}
            />
            <th className="col-status">Status</th>
            <th className="col-actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((record, i) => (
            <Row
              key={record.id}
              record={record}
              query={query}
              num={startIndex + i + 1}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
