import type { EmailDetail, SourceKey } from "@/lib/emails";
import { countryDisplay, genderDisplay, sentLabel } from "@/lib/display";
import CopyButton from "./CopyButton";
import MessageButton from "./MessageButton";
import SentToggle from "./SentToggle";

const SOURCE_LABELS: Record<string, string> = {
  discourse: "three.js",
  aboutme: "about.me",
};

function avatarGradient(seed: string): string {
  const colors: [string, string][] = [
    ["#6ea8fe", "#3b6fd4"],
    ["#b794f6", "#7c54c9"],
    ["#4ade80", "#22a35a"],
    ["#f472b6", "#c43d82"],
    ["#fbbf24", "#d18d09"],
    ["#38bdf8", "#0a82bd"],
  ];
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const [a, b] = colors[h % colors.length];
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

/** The inner detail view for a contact, shared by the full page and the slide-over drawer. */
export default function ContactDetail({ record }: { record: EmailDetail }) {
  const display = record.name || record.username || "Unknown";
  const initial = (display.trim()[0] || "?").toUpperCase();
  const src = record.source as SourceKey;
  const primaryEmail = record.emails[0] || "";
  const posts = record.postsFull;
  const country = countryDisplay(record.country, record.countryCode);
  const gender = genderDisplay(record.gender);
  const extLinks = [
    ...record.applyLinks.map((url) => ({ url, label: `Apply · ${hostOf(url)}` })),
    ...record.messaging.map((url) => ({ url, label: `Message · ${hostOf(url)}` })),
    ...record.links.map((url) => ({ url, label: hostOf(url) })),
  ];

  return (
    <>
      <header className="detail-head">
        <div className="avatar lg" style={{ background: avatarGradient(display) }}>
          {initial}
        </div>
        <div className="who">
          <div className="name">{display}</div>
          {record.title && <div className="position">{record.title}</div>}
          <div className="handle">
            {record.username ? `@${record.username}` : SOURCE_LABELS[src] ?? src}
          </div>
        </div>
        {record.messaged && (
          <span
            className="sent-badge"
            data-manual={record.messagedManual || undefined}
            title={sentLabel(record.messagedAt, record.messagedCount, record.messagedManual)}
          >
            ✓ {sentLabel(record.messagedAt, record.messagedCount, record.messagedManual)}
          </span>
        )}
        <span className="src-badge" data-source={src}>
          {SOURCE_LABELS[src] ?? src}
        </span>
        <SentToggle
          id={record.id}
          name={display}
          sent={record.messaged}
          variant="labeled"
        />
        {primaryEmail && (
          <MessageButton id={record.id} to={primaryEmail} name={display} variant="primary" />
        )}
      </header>

      {(record.organization || record.location || country || gender) && (
        <div className="card-meta">
          {record.organization && <span className="meta-bit">🏢 {record.organization}</span>}
          {country && <span className="meta-bit">{country}</span>}
          {gender && (
            <span className="gender-badge" data-gender={record.gender}>
              {gender}
            </span>
          )}
          {record.location && <span className="meta-bit">📍 {record.location}</span>}
        </div>
      )}

      {record.emails.length > 0 && (
        <div className="emails">
          {record.emails.map((email) => (
            <span className="email-chip" key={email}>
              <a href={`mailto:${email}`}>{email}</a>
              <CopyButton value={email} />
            </span>
          ))}
        </div>
      )}

      {record.tags.length > 0 && (
        <div className="tags">
          {record.tags.map((tag) => (
            <span className="tag" key={tag}>#{tag}</span>
          ))}
        </div>
      )}

      {extLinks.length > 0 && (
        <div className="ext-links">
          {extLinks.map((l) => (
            <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer">
              {l.label}
            </a>
          ))}
        </div>
      )}

      {posts.length > 0 && (
        <>
          <h2 className="posts-heading">
            {posts.length} {posts.length === 1 ? "post" : "posts"} from this contact
          </h2>
          <div className="posts-list">
            {posts.map((p, i) => {
              const date = formatDate(p.createdAt);
              return (
                <article className="post-item" key={`${p.url}-${i}`}>
                  <div className="post-head">
                    {p.url ? (
                      <a className="post-title" href={p.url} target="_blank" rel="noopener noreferrer">
                        {p.title || "(untitled)"}
                      </a>
                    ) : (
                      <span className="post-title">{p.title || "(untitled)"}</span>
                    )}
                    {date && <span className="post-date">🗓 {date}</span>}
                  </div>
                  {p.text && <div className="post-body">{p.text}</div>}
                </article>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}
