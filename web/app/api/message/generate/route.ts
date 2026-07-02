import { NextRequest, NextResponse } from "next/server";
import { API_BASE_URL } from "@/lib/emails";

export const dynamic = "force-dynamic";

/** Proxy to the Python server: generate a draft email with Claude. */
export async function POST(req: NextRequest) {
  const body = await req.text();
  try {
    const res = await fetch(`${API_BASE_URL}/api/message/generate`, {
      method: "POST",
      headers: { "content-type": "application/json" },
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
