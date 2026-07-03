import Link from "next/link";
import { listWorkspaces, loadWorkspace } from "@/lib/slack";
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
  const workspaces = await listWorkspaces();

  const requested = first(sp.ws);
  const active =
    workspaces.find((w) => w.slug === requested) ?? workspaces[0] ?? null;
  const data = active ? await loadWorkspace(active.slug) : null;

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
              <h1>Slack Users</h1>
              <p>
                {active
                  ? `${active.name} — ${active.count.toLocaleString()} members`
                  : "No workspace exports found"}
              </p>
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <Link href="/" className="tab">
              ← Contacts
            </Link>
            <ThemeToggle />
          </div>
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
              {workspaces.map((w) => (
                <Link
                  key={w.slug}
                  role="tab"
                  aria-selected={w.slug === active?.slug}
                  href={`/slack?ws=${encodeURIComponent(w.slug)}`}
                  className={`tab${w.slug === active?.slug ? " active" : ""}`}
                  scroll={false}
                >
                  {w.name}
                  <span className="tab-count">{w.count.toLocaleString()}</span>
                </Link>
              ))}
            </div>

            {data && (
              <SlackUsersTable columns={data.columns} rows={data.rows} />
            )}
          </>
        )}
      </main>
    </>
  );
}
