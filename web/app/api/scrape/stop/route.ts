import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL, API_HEADERS } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Stop a running re-scrape on the Python data server. */
export async function POST(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  try {
    const res = await fetch(`${API_BASE_URL}/api/scrape/stop?${qs}`, {
      method: "POST",
      headers: API_HEADERS,
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
