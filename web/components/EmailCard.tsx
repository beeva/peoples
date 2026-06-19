import type { EmailRecord, SourceKey } from "@/lib/emails";
import { highlight } from "@/lib/highlight";
import CopyButton from "./CopyButton";

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
  devto: "dev.to",
  aboutme: "about.me",
};

// What the title chip and "view original" link mean per source.
const TITLE_LABEL: Record<string, string> = {
  discourse: "Topic",
  devto: "Job",
  aboutme: "Role",
};
const ORIGIN_LABEL: Record<string, string> = {
  discourse: "View original post",
  devto: "View job post",
  aboutme: "View profile",
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

function hostOf(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function ExternalIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M15 3h6v6" />
      <path d="M10 14 21 3" />
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    </svg>
  );
}

export default function EmailCard({
  record,
  query,
}: {
  record: EmailRecord;
  query: string;
}) {
  const display = record.name || record.username || "Unknown";
  const initial = (display.trim()[0] || "?").toUpperCase();
  const src = record.source as SourceKey;
  const date = formatDate(record.createdAt);

  return (
    <article className="card">
      <div className="card-top">
        <div className="avatar" style={{ background: avatarGradient(display) }}>
          {initial}
        </div>
        <div className="who">
          <div className="name">{highlight(display, query)}</div>
          <div className="handle">
            {record.username ? `@${record.username}` : SOURCE_LABELS[src] ?? src}
          </div>
        </div>
        <span className="src-badge" data-source={src}>
          {SOURCE_LABELS[src] ?? src}
        </span>
      </div>

      {(record.organization || record.location || date) && (
        <div className="card-meta">
          {record.organization && (
            <span className="meta-bit">🏢 {highlight(record.organization, query)}</span>
          )}
          {record.location && (
            <span className="meta-bit">📍 {highlight(record.location, query)}</span>
          )}
          {date && <span className="meta-bit">🗓 {date}</span>}
        </div>
      )}

      {record.title && (
        <a
          className="topic"
          href={record.url || undefined}
          target="_blank"
          rel="noopener noreferrer"
          title={`${TITLE_LABEL[src] ?? ""}: ${record.title}`}
        >
          {highlight(record.title, query)}
        </a>
      )}

      {record.emails.length > 0 && (
        <div className="emails">
          {record.emails.map((email) => (
            <span className="email-chip" key={email}>
              <a href={`mailto:${email}`}>{highlight(email, query)}</a>
              <CopyButton value={email} />
            </span>
          ))}
        </div>
      )}

      {record.tags.length > 0 && (
        <div className="tags">
          {record.tags.slice(0, 6).map((tag) => (
            <span className="tag" key={tag}>
              #{tag}
            </span>
          ))}
        </div>
      )}

      {record.preview && (
        <p className="preview">{highlight(record.preview, query)}</p>
      )}

      {(record.applyLinks.length > 0 || record.messaging.length > 0) && (
        <div className="ext-links">
          {record.applyLinks.map((url) => (
            <a key={url} href={url} target="_blank" rel="noopener noreferrer">
              <ExternalIcon /> Apply · {hostOf(url)}
            </a>
          ))}
          {record.messaging.map((url) => (
            <a key={url} href={url} target="_blank" rel="noopener noreferrer">
              <ExternalIcon /> Message · {hostOf(url)}
            </a>
          ))}
        </div>
      )}

      {record.url && (
        <div className="card-foot">
          <a href={record.url} target="_blank" rel="noopener noreferrer">
            <ExternalIcon />
            {ORIGIN_LABEL[src] ?? "View original"}
          </a>
        </div>
      )}
    </article>
  );
}
