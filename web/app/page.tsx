import {
  ageActive,
  DEFAULT_SORT,
  fetchEmails,
  isSortKey,
  parseFilter,
  parseMessaged,
  PER_PAGE,
  type SortKey,
  type SourceKey,
} from "@/lib/emails";
import EmailTable from "@/components/EmailTable";
import Pagination from "@/components/Pagination";
import SearchControls from "@/components/SearchControls";
import CategoryTabs from "@/components/CategoryTabs";
import StatusFilter from "@/components/StatusFilter";
import FacetFilters from "@/components/FacetFilters";
import RescrapeButton from "@/components/RescrapeButton";
import ExportButton from "@/components/ExportButton";
import { SelectionProvider } from "@/components/Selection";
import SelectionBar from "@/components/SelectionBar";
import ThemeToggle from "@/components/ThemeToggle";
import Toaster from "@/components/Toaster";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;

const VALID_SOURCES = new Set(["all", "discourse", "aboutme", "github"]);

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
  const sortParam = first(sp.sort);
  const sort: SortKey = isSortKey(sortParam) ? sortParam : DEFAULT_SORT;
  const page = Math.max(1, parseInt(first(sp.page) || "1", 10) || 1);
  const sourceParam = first(sp.source);
  const source: SourceKey = VALID_SOURCES.has(sourceParam) ? sourceParam : "all";
  const messaged = parseMessaged(first(sp.messaged));
  const filter = parseFilter(sp);

  const result = await fetchEmails(
    source, q, sort, page, PER_PAGE, messaged, filter,
  );
  const stats = result.stats;
  const noun = stats.noun || "Records";
  const activeSource = result.sources.find((s) => s.key === result.source);

  const from = result.total ? (result.page - 1) * result.perPage + 1 : 0;
  const to = from + result.items.length - 1;

  const baseParams: Record<string, string> = {};
  if (q) baseParams.q = q;
  if (sort !== DEFAULT_SORT) baseParams.sort = sort;
  if (result.source !== "all") baseParams.source = result.source;
  if (result.messaged !== "all") baseParams.messaged = result.messaged;
  // Keep the active country/gender/age filter in pagination and sort links.
  if (filter.countries.length) baseParams.country = filter.countries.join(",");
  if (filter.genders.length) baseParams.gender = filter.genders.join(",");
  if (filter.contactable.length)
    baseParams.contactable = filter.contactable.join(",");
  // Use the runs the server actually applied, not the raw URL value: a step that
  // was merged away is dropped, so links don't re-add a phantom `runs=` filter.
  if (result.filter.runs.length) baseParams.runs = result.filter.runs.join(",");
  if (ageActive(filter)) {
    baseParams.age_op = filter.ageOp;
    baseParams.age = filter.ageValue;
  }
  if (filter.joinedOp && filter.joinedDate) {
    baseParams.joined_op = filter.joinedOp;
    baseParams.joined_date = filter.joinedDate;
  }
  if (filter.activeOp && filter.activeDate) {
    baseParams.active_op = filter.activeOp;
    baseParams.active_date = filter.activeDate;
  }

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
          {/* Brand lives in the sidebar now; the topbar names the page. */}
          <div className="brand">
            <div>
              <h1>Contacts</h1>
              <p>{subtitle}</p>
            </div>
          </div>
          <ThemeToggle />
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
          {/* CSV export of main emails -- GitHub first; other sources later. */}
          {result.source === "github" && activeSource && (
            <ExportButton source={result.source} label={activeSource.label} />
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

        <FacetFilters
          filter={result.filter}
          facets={result.facets}
          showAge={result.source === "github"}
          mergeSource={result.source === "github" ? "github" : ""}
        />

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
          // The table is a server component; only the tick boxes and the bar
          // that acts on them run in the browser.
          <SelectionProvider ids={result.items.map((item) => item.id)}>
            <SelectionBar />
            <EmailTable
              items={result.items}
              query={q}
              startIndex={(result.page - 1) * result.perPage}
              sort={sort}
              baseParams={baseParams}
            />
          </SelectionProvider>
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
