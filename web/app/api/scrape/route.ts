import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Trigger an incremental re-scrape of a source on the Python data server. */
export async function POST(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  try {
    const res = await fetch(`${API_BASE_URL}/api/scrape?${qs}`, {
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

/** Poll the status of a source's scrape job. */
export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  try {
    const res = await fetch(`${API_BASE_URL}/api/scrape/status?${qs}`, {
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { status: "idle", error: `Could not reach data server (${message}).` },
      { status: 502 },
    );
  }
}
