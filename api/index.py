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

handler = server.Handler
