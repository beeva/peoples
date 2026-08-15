import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/** HTTP Basic auth over the whole app, when UI_USER and UI_PASS are set.
 *
 *  Unset means local development, where the app is on localhost and a login
 *  would only be in the way. Set them on the deployment: this directory holds
 *  names, addresses and phone numbers for thousands of people who did not opt
 *  in to being in it, and a public URL turns private research into publishing.
 *
 *  Basic auth is chosen because it needs no session store, no database and no
 *  third-party service -- it is the smallest thing that keeps the directory
 *  private, and it works identically on every host.
 */
export function middleware(req: NextRequest) {
  const user = process.env.UI_USER;
  const pass = process.env.UI_PASS;
  if (!user || !pass) return NextResponse.next();

  const header = req.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    // atob rather than Buffer: middleware runs on the edge runtime, which has
    // no Node globals.
    const [sentUser, ...rest] = atob(header.slice(6)).split(":");
    // Compare both halves in full so a correct username with a wrong password
    // is no more informative than a wrong username.
    if (sentUser === user && rest.join(":") === pass) {
      return NextResponse.next();
    }
  }
  return new NextResponse("Authentication required", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="peoples", charset="UTF-8"',
    },
  });
}

export const config = {
  // Everything except Next's own static output. The API routes under /api are
  // deliberately included -- they proxy to the data server, so leaving them
  // open would leave the data open.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"],
};
