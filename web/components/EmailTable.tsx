import Link from "next/link";
import type { EmailRecord, SourceKey } from "@/lib/emails";
import { highlight } from "@/lib/highlight";
import { countryDisplay, genderDisplay, sentLabel } from "@/lib/display";
import CopyButton from "./CopyButton";
import MessageButton from "./MessageButton";

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

function Row({ record, query }: { record: EmailRecord; query: string }) {
  const display = record.name || record.username || "Unknown";
  const initial = (display.trim()[0] || "?").toUpperCase();
  const src = record.source as SourceKey;
  const date = formatDate(record.createdAt);
  const href = `/contact/${encodeURIComponent(record.id)}`;
  const primaryEmail = record.emails[0] || "";
  const extraEmails = record.emails.length - 1;
  const country = countryDisplay(record.country, record.countryCode);
  const gender = genderDisplay(record.gender);

  return (
    <tr className={record.messaged ? "messaged" : undefined}>
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
        {!record.organization && !country && !record.location && (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-summary">
        {record.preview ? (
          <span className="summary-text">{highlight(record.preview, query)}</span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="col-date">{date || <span className="muted">—</span>}</td>

      <td className="col-status">
        {record.messaged ? (
          <span
            className="sent-badge"
            title={sentLabel(record.messagedAt, record.messagedCount)}
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

export default function EmailTable({
  items,
  query,
}: {
  items: EmailRecord[];
  query: string;
}) {
  return (
    <div className="table-wrap">
      <table className="contact-table">
        <thead>
          <tr>
            <th className="col-contact">Contact</th>
            <th className="col-gender">Gender</th>
            <th className="col-source">Source</th>
            <th className="col-email">Email</th>
            <th className="col-meta">Details</th>
            <th className="col-summary">Summary</th>
            <th className="col-date">Date</th>
            <th className="col-status">Status</th>
            <th className="col-actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((record) => (
            <Row key={record.id} record={record} query={query} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
