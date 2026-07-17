import Link from "next/link";
import { countAllUsers, listWorkspaces, loadView } from "@/lib/slack";
import SlackUsersTable from "@/components/SlackUsersTable";
import ThemeToggle from "@/components/ThemeToggle";

type SearchParams = Promise<{ [key: string]: string | string[] | undefined }>;

function first(v: string | string[] | undefined): string {
  return Array.isArray(v) ? (v[0] ?? "") : (v ?? "");
}

export default async function SlackPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const sp = await searchParams;
  const [workspaces, allCount] = await Promise.all([
    listWorkspaces(),
    countAllUsers(),
  ]);

  const requested = first(sp.ws) || "all";
  const view =
    requested === "all" || workspaces.some((w) => w.slug === requested)
      ? requested
      : "all";
  const data = await loadView(view);

  const tabs = [
    { slug: "all", name: "All Users", count: allCount },
    ...workspaces.map((w) => ({ slug: w.slug, name: w.name, count: w.count })),
  ];

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          {/* Brand lives in the sidebar now; the topbar names the page. */}
          <div className="brand">
            <div>
              <h1>Slack Users</h1>
              <p>
                {data
                  ? `${data.name} — ${data.count.toLocaleString()} ${
                      view === "all" ? "unique people" : "members"
                    }`
                  : "No workspace exports found"}
              </p>
            </div>
          </div>
          <ThemeToggle />
        </div>
      </header>

      <main className="wrap">
        {workspaces.length === 0 ? (
          <div className="empty">
            <div>
              No workspace files found in <code>scrapers/slack/users</code>.
            </div>
          </div>
        ) : (
          <>
            <div className="tabs" role="tablist" aria-label="Slack workspace">
              {tabs.map((t) => (
                <Link
                  key={t.slug}
                  role="tab"
                  aria-selected={t.slug === view}
                  href={`/slack?ws=${encodeURIComponent(t.slug)}`}
                  className={`tab${t.slug === view ? " active" : ""}`}
                  scroll={false}
                >
                  {t.name}
                  <span className="tab-count">{t.count.toLocaleString()}</span>
                </Link>
              ))}
            </div>

            {data && (
              <SlackUsersTable
                columns={data.columns}
                users={data.users}
                view={view}
              />
            )}
          </>
        )}
      </main>
    </>
  );
}
