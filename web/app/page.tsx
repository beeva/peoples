import Link from "next/link";
import {
  fetchEmails,
  parseMessaged,
  PER_PAGE,
  type SourceKey,
} from "@/lib/emails";
import EmailTable from "@/components/EmailTable";
import Pagination from "@/components/Pagination";
import SearchControls from "@/components/SearchControls";
import CategoryTabs from "@/components/CategoryTabs";
import StatusFilter from "@/components/StatusFilter";
import RescrapeButton from "@/components/RescrapeButton";
import ThemeToggle from "@/components/ThemeToggle";
import Toaster from "@/components/Toaster";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;

const VALID_SOURCES = new Set(["all", "discourse", "aboutme"]);

function first(v: string | string[] | undefined): string {
  return Array.isArray(v) ? (v[0] ?? "") : (v ?? "");
}

function StatCard({ num, label }: { num: string; label: string }) {
  return (
    <div className="stat">
      <div className="num">{num}</div>
      <div className="lbl">{label}</div>
    </div>
  );
}

export default async function Home({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const sp = await searchParams;
  const q = first(sp.q);
  const sort = first(sp.sort) === "oldest" ? "oldest" : "newest";
  const page = Math.max(1, parseInt(first(sp.page) || "1", 10) || 1);
  const sourceParam = first(sp.source);
  const source: SourceKey = VALID_SOURCES.has(sourceParam) ? sourceParam : "all";
  const messaged = parseMessaged(first(sp.messaged));

  const result = await fetchEmails(source, q, sort, page, PER_PAGE, messaged);
  const stats = result.stats;
  const noun = stats.noun || "Records";
  const activeSource = result.sources.find((s) => s.key === result.source);

  const from = result.total ? (result.page - 1) * result.perPage + 1 : 0;
  const to = from + result.items.length - 1;

  const baseParams: Record<string, string> = {};
  if (q) baseParams.q = q;
  if (sort !== "newest") baseParams.sort = sort;
  if (result.source !== "all") baseParams.source = result.source;
  if (result.messaged !== "all") baseParams.messaged = result.messaged;

  const range =
    stats.earliest && stats.latest
      ? `${stats.earliest} – ${stats.latest}`
      : "—";

  const subtitle = activeSource
    ? activeSource.key === "all"
      ? "Contacts scraped from three.js & about.me"
      : `Contacts from ${activeSource.label}`
    : "Scraped contact directory";

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <div className="logo">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="#fff"
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M12 2 2 7l10 5 10-5-10-5Z" />
                <path d="m2 17 10 5 10-5" />
                <path d="m2 12 10 5 10-5" />
              </svg>
            </div>
            <div>
              <h1>Contact Directory</h1>
              <p>{subtitle}</p>
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <Link href="/slack" className="tab">
              Slack Users
            </Link>
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="wrap">
        <div className="toolbar">
          <CategoryTabs sources={result.sources} active={result.source} />
          {result.source !== "all" && activeSource && (
            <RescrapeButton
              source={result.source as Exclude<typeof result.source, "all">}
              label={activeSource.label}
            />
          )}
        </div>

        <section className="stats">
          <StatCard num={stats.totalPosts.toLocaleString()} label={noun} />
          <StatCard
            num={stats.uniqueEmails.toLocaleString()}
            label="Unique emails"
          />
          <StatCard
            num={stats.totalEmails.toLocaleString()}
            label="Email mentions"
          />
          <StatCard num={range} label="Date range" />
        </section>

        <SearchControls initialQuery={q} initialSort={sort} />

        <div className="filter-row">
          <StatusFilter active={result.messaged} counts={result.messagedCounts} />
        </div>

        {result.error && (
          <div className="banner" role="alert">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
              <path d="M12 9v4M12 17h.01" />
            </svg>
            <span>
              {result.error} Start it with <code>python server.py</code> and
              refresh.
            </span>
          </div>
        )}

        <div className="result-meta">
          {result.total > 0 ? (
            <>
              Showing{" "}
              <b>
                {from.toLocaleString()}–{to.toLocaleString()}
              </b>{" "}
              of <b>{result.total.toLocaleString()}</b> {q ? "matching " : ""}
              {noun.toLowerCase()}
            </>
          ) : null}
        </div>

        {result.items.length > 0 ? (
          <EmailTable items={result.items} query={q} />
        ) : (
          <div className="empty">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.6}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="11" cy="11" r="8" />
              <path d="m21 21-4.3-4.3" />
            </svg>
            <div>No results found{q ? ` for “${q}”` : ""}.</div>
          </div>
        )}

        <Pagination
          page={result.page}
          totalPages={result.totalPages}
          baseParams={baseParams}
        />
      </main>

      <Toaster />
    </>
  );
}
