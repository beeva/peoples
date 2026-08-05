import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL } from "@/lib/emails";

export const dynamic = "force-dynamic";
// A restore replaces the whole archive; it is allowed to take a while.
export const maxDuration = 300;

/** Restore a .sql dump.
 *
 *  Two forms, because the two cases are genuinely different: a dump already on
 *  the server (picked from the backups list) is named by path and never
 *  travels anywhere, while a file chosen from the user's own machine is
 *  forwarded as raw bytes.
 */
export async function POST(req: NextRequest) {
  const contentType = req.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  try {
    const res = await fetch(`${API_BASE_URL}/api/db/import`, {
      method: "POST",
      headers: { "Content-Type": isJson ? "application/json" : "application/sql" },
      body: isJson ? await req.text() : await req.arrayBuffer(),
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { ok: false, error: `Could not reach data server (${message}).` },
      { status: 502 },
    );
  }
}
