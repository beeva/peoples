"""Vercel Python entry point: serve server.py's Handler as a serverless function.

Vercel's Python runtime looks for a module-level `handler` that subclasses
BaseHTTPRequestHandler -- which is exactly what server.py already defines, so
this file is an adapter rather than a second implementation of the API.

Only needed for the all-on-Vercel deployment (see VERCEL.md, Appendix A). The
recommended layout runs server.py as a long-lived container instead, because a
serverless function cannot host the background enrichment workers.
"""
import os
import sys

# The project's modules live one directory up; a function's working directory
# is not the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# server first, and the order matters: importing it loads `.env` into the
# environment and only then imports db, whose module-level MYSQL_* config is
# read once at import time. Importing db first would freeze it against an
# environment that has not been populated yet -- invisible on Vercel, where the
# platform supplies the variables, and a connection refused everywhere else.
import server      # noqa: E402
import db          # noqa: E402  -- already imported by server; same module object

# Ten CREATE TABLE IF NOT EXISTS statements plus a migration check, each a
# round-trip to a database on the other side of the internet -- affordable once,
# wasteful on every cold start. Set SKIP_BOOTSTRAP=1 after the first successful
# deploy, once the schema exists.
if os.environ.get("SKIP_BOOTSTRAP", "").strip() != "1":
    db.bootstrap(verbose=False)

# STATE defaults to {} at import, so this is not strictly required -- but the
# scrape-status routes read it, and one query at cold start is cheaper than
# serving a page that reports every source as never having run.
server.load_state()


class Handler(server.Handler):
    """The real Handler, with one deployment-specific diagnostic.

    Routing is done on `self.path`, and `vercel.json` rewrites every `/api/*`
    request to this function. A rewrite is meant to be internal -- the function
    should still see the original URL -- but if a platform ever delivered the
    *destination* path instead, every route would 404 with nothing to explain
    why. Echoing the path that actually arrived turns that from a debugging
    session into a glance.
    """

    def _send_json(self, obj, status=200):
        if status == 404 and isinstance(obj, dict) and obj.get("error") == "not found":
            obj = {**obj, "received_path": self.path}
        return super()._send_json(obj, status)


handler = Handler
