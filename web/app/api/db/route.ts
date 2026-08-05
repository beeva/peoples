import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Database status: server, size, per-table counts, sync state, dumps on disk. */
export async function GET() {
  try {
    const res = await fetch(`${API_BASE_URL}/api/db/status`, {
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { connected: false, error: `Could not reach data server (${message}).` },
      { status: 502 },
    );
  }
}

/** Import any pre-database scraper files still on disk (`?force=1` to redo). */
export async function POST(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  try {
    const res = await fetch(`${API_BASE_URL}/api/db/import-files?${qs}`, {
      method: "POST",
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
