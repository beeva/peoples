import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Fetch filtered export rows (the CSV preview) from the Python data server.
 *  The query string is forwarded verbatim -- including repeated `position`
 *  params, which is why it isn't rebuilt from parsed values here. */
export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  try {
    const res = await fetch(`${API_BASE_URL}/api/export?${qs}`, {
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
