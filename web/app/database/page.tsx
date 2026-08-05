import DatabasePanel, { DbStatus } from "@/components/DatabasePanel";
import ThemeToggle from "@/components/ThemeToggle";
import { API_BASE_URL } from "@/lib/emails";

export const dynamic = "force-dynamic";

async function loadStatus(): Promise<DbStatus> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/db/status`, { cache: "no-store" });
    if (!res.ok) throw new Error(`Data server responded ${res.status}`);
    return (await res.json()) as DbStatus;
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return {
      connected: false,
      error: `Could not reach the data server at ${API_BASE_URL} (${message}).`,
    };
  }
}

export default async function DatabasePage() {
  const status = await loadStatus();

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <div>
              <h1>Database</h1>
              <p>
                {status.connected
                  ? `MySQL ${status.server} — ${status.database}`
                  : "Not connected"}
              </p>
            </div>
          </div>
          <ThemeToggle />
        </div>
      </header>

      <main className="wrap">
        <DatabasePanel initial={status} />
      </main>
    </>
  );
}
