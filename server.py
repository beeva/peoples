#!/usr/bin/env python3
"""Lightweight zero-dependency server for browsing scraped contacts.

Serves a single-page frontend (index.html) plus a small JSON API with
pagination, full-text search, sorting, and **multiple data sources**:

    * discourse  -> scrapers/discourse/threejs/threejs_emails.jsonl
    * devto      -> scrapers/devto/jobs.json
    * aboutme    -> scrapers/aboutme/users.jsonl

Each source is normalised into one common record shape (with a `source` tag),
so the frontend can show them together or one category at a time.

No third-party packages required:

    python server.py            # http://127.0.0.1:8000
    python server.py 9000       # custom port
"""
import json
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
MAX_PER_PAGE = 100

DISCOURSE_FILE = BASE_DIR / "scrapers" / "discourse" / "threejs" / "threejs_emails.jsonl"
DEVTO_FILE = BASE_DIR / "scrapers" / "devto" / "jobs.json"
ABOUTME_FILE = BASE_DIR / "scrapers" / "aboutme" / "users.jsonl"

# Strip HTML tags to build a clean text preview / searchable blob.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MD_RE = re.compile(r"[#>*_`~\[\]()!]+")  # light markdown noise


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return _WS_RE.sub(" ", text).strip()


def _preview(text: str, limit: int = 320) -> str:
    text = _strip_html(text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _md_preview(markdown: str, limit: int = 320) -> str:
    text = _MD_RE.sub(" ", markdown or "")
    return _preview(text, limit)


def _parse_ts(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %Y")


def _read_jsonl(path: Path):
    if not path.exists():
        print(f"[warn] data file not found: {path}", file=sys.stderr)
        return
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"[warn] {path.name}: skipping malformed line {line_no}", file=sys.stderr)


def _read_json_array(path: Path):
    if not path.exists():
        print(f"[warn] data file not found: {path}", file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[warn] {path.name}: malformed JSON", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def _norm_tags(tags) -> list[str]:
    """dev.to tag_list is sometimes a list, sometimes a comma string."""
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in (tags or []) if str(t).strip()]


def _norm_links(links) -> list[str]:
    """about.me links are dicts (or strings); pull out URL-ish values."""
    out = []
    for it in links or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in ("url", "href", "link", "value"):
                v = it.get(k)
                if v:
                    out.append(v)
                    break
    return [s for s in out if s]


# ---- common record shape --------------------------------------------------
def _record(source, local_id, *, emails, name="", username="", title="",
            url="", created_at="", preview="", tags=None, location="",
            organization="", apply_links=None, messaging=None, links=None,
            search_extra=""):
    emails = [e.lower() for e in (emails or []) if e]
    # de-dupe while preserving order
    seen, uniq = set(), []
    for e in emails:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    rec = {
        "id": f"{source}:{local_id}",
        "source": source,
        "emails": uniq,
        "name": (name or "").strip(),
        "username": (username or "").strip(),
        "title": (title or "").strip(),
        "url": url or "",
        "created_at": created_at or "",
        "preview": preview or "",
        "tags": tags or [],
        "location": (location or "").strip(),
        "organization": (organization or "").strip(),
        "apply_links": apply_links or [],
        "messaging": messaging or [],
        "links": links or [],
    }
    rec["_ts"] = _parse_ts(created_at or "")
    rec["_blob"] = " ".join([
        " ".join(uniq), rec["name"], rec["username"], rec["title"],
        rec["location"], rec["organization"], " ".join(rec["tags"]),
        rec["preview"], search_extra,
    ]).lower()
    return rec


# ---- per-source loaders ---------------------------------------------------
def load_discourse():
    records = []
    for i, d in enumerate(_read_jsonl(DISCOURSE_FILE), 1):
        records.append(_record(
            "discourse", d.get("post_id", i),
            emails=d.get("emails", []),
            name=d.get("name") or "",
            username=d.get("username") or "",
            title=d.get("topic_title", ""),
            url=d.get("post_url") or d.get("topic_url") or "",
            created_at=d.get("created_at", ""),
            preview=_preview(d.get("cooked", "")),
            search_extra=d.get("topic_title", ""),
        ))
    return records


def load_devto():
    records = []
    for i, d in enumerate(_read_json_array(DEVTO_FILE), 1):
        contact = d.get("contact") or {}
        emails = list(contact.get("emails", [])) + list(contact.get("mailto", []))
        records.append(_record(
            "devto", d.get("id", i),
            emails=emails,
            name=d.get("author") or "",
            title=d.get("title", ""),
            url=d.get("url") or "",
            created_at=d.get("published_at", ""),
            preview=_md_preview(d.get("description", "")),
            tags=_norm_tags(d.get("tags")),
            organization=d.get("organization") or "",
            apply_links=contact.get("apply_links", []),
            messaging=contact.get("messaging", []),
        ))
    return records


def load_aboutme():
    records = []
    for i, d in enumerate(_read_jsonl(ABOUTME_FILE), 1):
        username = d.get("username") or str(d.get("user_id") or i)
        records.append(_record(
            "aboutme", username,
            emails=d.get("emails", []),
            name=d.get("full_name") or username,
            username=username,
            title=d.get("role") or "",
            url=d.get("url") or "",
            created_at="",  # about.me profiles have no timestamp
            preview=_preview(d.get("summary") or ""),
            location=d.get("location") or "",
            links=_norm_links(d.get("links")),
            search_extra=" ".join(d.get("schools", []) + d.get("interests", [])),
        ))
    return records


# ---- source registry ------------------------------------------------------
SOURCES = [
    {"key": "discourse", "label": "three.js forum", "noun": "Posts", "loader": load_discourse},
    {"key": "devto", "label": "dev.to jobs", "noun": "Jobs", "loader": load_devto},
    {"key": "aboutme", "label": "about.me", "noun": "Profiles", "loader": load_aboutme},
]
NOUNS = {s["key"]: s["noun"] for s in SOURCES}
NOUNS["all"] = "Records"

# Load every source once at startup; keep an "all" combined list too.
BY_SOURCE = {s["key"]: s["loader"]() for s in SOURCES}
ALL_RECORDS = [r for s in SOURCES for r in BY_SOURCE[s["key"]]]


def _stats_for(records, noun):
    ts_values = [r["_ts"] for r in records if r["_ts"]]
    unique = {e for r in records for e in r["emails"]}
    return {
        "total_posts": len(records),
        "total_emails": sum(len(r["emails"]) for r in records),
        "unique_emails": len(unique),
        "earliest": _fmt_date(min(ts_values)) if ts_values else None,
        "latest": _fmt_date(max(ts_values)) if ts_values else None,
        "noun": noun,
    }


# Pre-compute stats + the source list (with counts) used by the selector.
STATS_BY_SOURCE = {"all": _stats_for(ALL_RECORDS, NOUNS["all"])}
for s in SOURCES:
    STATS_BY_SOURCE[s["key"]] = _stats_for(BY_SOURCE[s["key"]], s["noun"])

SOURCE_LIST = [{"key": "all", "label": "All", "noun": NOUNS["all"],
                "count": len(ALL_RECORDS)}]
for s in SOURCES:
    SOURCE_LIST.append({
        "key": s["key"], "label": s["label"], "noun": s["noun"],
        "count": len(BY_SOURCE[s["key"]]),
    })

# Fields exposed to the client (drop internal _ts / _blob).
PUBLIC_FIELDS = (
    "id", "source", "emails", "name", "username", "title", "url",
    "created_at", "preview", "tags", "location", "organization",
    "apply_links", "messaging", "links",
)


def query_records(source: str, q: str, sort: str, page: int, per_page: int):
    items = BY_SOURCE.get(source) if source != "all" else ALL_RECORDS
    if items is None:  # unknown source -> behave like "all"
        source = "all"
        items = ALL_RECORDS

    q = (q or "").strip().lower()
    if q:
        terms = q.split()
        items = [r for r in items if all(t in r["_blob"] for t in terms)]

    reverse = sort != "oldest"
    items = sorted(items, key=lambda r: r["_ts"], reverse=reverse)

    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    window = items[start:start + per_page]
    payload = [{k: r.get(k) for k in PUBLIC_FIELDS} for r in window]

    return {
        "items": payload,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "source": source,
        "stats": STATS_BY_SOURCE.get(source, STATS_BY_SOURCE["all"]),
        "sources": SOURCE_LIST,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ContactDirectory/2.0"

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if route == "/api/stats":
            params = parse_qs(parsed.query)
            source = params.get("source", ["all"])[0]
            self._send_json({
                "stats": STATS_BY_SOURCE.get(source, STATS_BY_SOURCE["all"]),
                "sources": SOURCE_LIST,
            })
            return

        if route == "/api/emails":
            params = parse_qs(parsed.query)
            page = _to_int(params.get("page", ["1"])[0], 1)
            per_page = _to_int(params.get("per_page", ["12"])[0], 12)
            per_page = max(1, min(per_page, MAX_PER_PAGE))
            q = params.get("q", [""])[0]
            sort = params.get("sort", ["newest"])[0]
            source = params.get("source", ["all"])[0]
            self._send_json(query_records(source, q, sort, page, per_page))
            return

        self._send_json({"error": "not found"}, 404)


def _to_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main():
    port = _to_int(sys.argv[1], 8000) if len(sys.argv) > 1 else 8000
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, Handler)
    counts = ", ".join(f"{s['key']}={len(BY_SOURCE[s['key']])}" for s in SOURCES)
    print(f"Loaded {len(ALL_RECORDS)} records ({counts})")
    print(f"Serving on http://{addr[0]}:{addr[1]}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
