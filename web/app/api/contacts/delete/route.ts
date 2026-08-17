import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL, API_HEADERS } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Proxy to the Python server: delete one contact, or every selected one.
 *
 *  Body is `{ id }` from a row's delete button or `{ ids }` from the selection
 *  bar; the data server treats the first as a list of one.
 */
export async function POST(req: NextRequest) {
  const body = await req.text();
  try {
    const res = await fetch(`${API_BASE_URL}/api/contacts/delete`, {
      method: "POST",
      headers: { ...API_HEADERS, "content-type": "application/json" },
      body,
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
