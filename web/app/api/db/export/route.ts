import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL, API_HEADERS } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Dump the database and stream the .sql file back as a download.
 *
 *  The body is piped straight through rather than buffered -- a dump of a real
 *  archive runs to tens of megabytes, and there is no reason for it to sit in
 *  this process's memory on the way past.
 */
export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  try {
    const res = await fetch(`${API_BASE_URL}/api/db/export?${qs}`, {
      headers: API_HEADERS,
      cache: "no-store",
    });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({ error: "export failed" }));
      return NextResponse.json(data, { status: res.status || 500 });
    }
    const headers = new Headers();
    for (const key of ["content-type", "content-disposition", "content-length"]) {
      const value = res.headers.get(key);
      if (value) headers.set(key, value);
    }
    headers.set("Cache-Control", "no-store");
    return new NextResponse(res.body, { status: 200, headers });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { ok: false, error: `Could not reach data server (${message}).` },
      { status: 502 },
    );
  }
}
