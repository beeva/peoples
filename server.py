#!/usr/bin/env python3
"""Lightweight zero-dependency server for browsing scraped contacts.

Serves a single-page frontend (index.html) plus a small JSON API with
pagination, full-text search, sorting, **multiple data sources**, and
**on-demand incremental re-scraping** of each source:

    * discourse  -> scrapers/discourse/threejs/threejs_emails.jsonl
    * devto      -> scrapers/devto/jobs.json
    * aboutme    -> scrapers/aboutme/users.jsonl

Each source is normalised into one common record shape (with a `source` tag).
A re-scrape runs the source's scraper as a background subprocess; every scraper
is incremental (it skips content it already has), so re-scraping only fetches
what is new. After a scrape finishes the source is hot-reloaded in memory.

No third-party packages required:

    python server.py            # http://127.0.0.1:8000
    python server.py 9000       # custom port
"""
import json
import re
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
SCRAPERS_DIR = BASE_DIR / "scrapers"
STATE_FILE = SCRAPERS_DIR / "state.json"
MAX_PER_PAGE = 100

DISCOURSE_FILE = SCRAPERS_DIR / "discourse" / "threejs" / "threejs_emails.jsonl"
DEVTO_FILE = SCRAPERS_DIR / "devto" / "jobs.json"
ABOUTME_FILE = SCRAPERS_DIR / "aboutme" / "users.jsonl"

# Per-source resume cursors written by the scrapers (git-ignored runtime state).
DISCOURSE_CURSOR = SCRAPERS_DIR / "discourse" / "threejs" / ".cursor"
ABOUTME_CURSOR = SCRAPERS_DIR / "aboutme" / ".cursor"
CURSOR_FILES = {"discourse": DISCOURSE_CURSOR, "aboutme": ABOUTME_CURSOR}

# Strip HTML tags to build a clean text preview / searchable blob.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MD_RE = re.compile(r"[#>*_`~\[\]()!]+")  # light markdown noise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}
NOUNS = {s["key"]: s["noun"] for s in SOURCES}
NOUNS["all"] = "Records"

PUBLIC_FIELDS = (
    "id", "source", "emails", "name", "username", "title", "url",
    "created_at", "preview", "tags", "location", "organization",
    "apply_links", "messaging", "links",
)

# In-memory dataset, guarded by DATA_LOCK (ThreadingHTTPServer is multi-threaded).
DATA_LOCK = threading.RLock()
BY_SOURCE = {}
ALL_RECORDS = []
STATS_BY_SOURCE = {}
SOURCE_LIST = []


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


def _rebuild_aggregates():
    """Recompute ALL_RECORDS / stats / source list from BY_SOURCE. Hold DATA_LOCK."""
    global ALL_RECORDS, STATS_BY_SOURCE, SOURCE_LIST
    ALL_RECORDS = [r for s in SOURCES for r in BY_SOURCE.get(s["key"], [])]
    STATS_BY_SOURCE = {"all": _stats_for(ALL_RECORDS, NOUNS["all"])}
    for s in SOURCES:
        STATS_BY_SOURCE[s["key"]] = _stats_for(BY_SOURCE.get(s["key"], []), s["noun"])
    SOURCE_LIST = [{"key": "all", "label": "All", "noun": NOUNS["all"],
                    "count": len(ALL_RECORDS)}]
    for s in SOURCES:
        SOURCE_LIST.append({
            "key": s["key"], "label": s["label"], "noun": s["noun"],
            "count": len(BY_SOURCE.get(s["key"], [])),
        })


def reload_source(key: str) -> int:
    """Re-read one source from disk and rebuild aggregates. Returns its count."""
    with DATA_LOCK:
        BY_SOURCE[key] = SOURCE_BY_KEY[key]["loader"]()
        _rebuild_aggregates()
        return len(BY_SOURCE[key])


def load_all():
    with DATA_LOCK:
        for s in SOURCES:
            BY_SOURCE[s["key"]] = s["loader"]()
        _rebuild_aggregates()


# ---- persisted per-source state (last_run, counts, cursor) ----------------
STATE_LOCK = threading.Lock()
STATE = {}


def load_state():
    global STATE
    try:
        STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(STATE, dict):
            STATE = {}
    except (OSError, json.JSONDecodeError):
        STATE = {}


def save_state():
    with STATE_LOCK:
        try:
            STATE_FILE.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[warn] could not write {STATE_FILE.name}: {e}", file=sys.stderr)


# ---- scrape jobs ----------------------------------------------------------
JOBS_LOCK = threading.Lock()
JOBS = {}  # source -> job dict


def _seed_discourse_cursor() -> int:
    """Highest topic_id already in the emails file -> first-run resume point,
    so we don't re-scrape the topics we already have."""
    best = 0
    for d in _read_jsonl(DISCOURSE_FILE):
        tid = d.get("topic_id")
        if isinstance(tid, int) and tid > best:
            best = tid
    return best


def _scrape_argv(key: str, params: dict) -> list[str]:
    """Build the bounded, incremental command for a source's scraper."""
    py = sys.executable
    if key == "discourse":
        limit = params.get("limit", "300")
        cursor = (STATE.get("discourse") or {}).get("cursor")
        if cursor in (None, ""):
            cursor = _seed_discourse_cursor()
        return [py, str(SCRAPERS_DIR / "discourse" / "discourse_scrape.py"),
                "--limit", str(limit),
                "--since-topic-id", str(cursor or 0),
                "--cursor-out", str(DISCOURSE_CURSOR)]
    if key == "devto":
        pages = params.get("pages", "3")
        argv = [py, str(SCRAPERS_DIR / "devto" / "devto_scrape.py"), "--pages", str(pages)]
        if params.get("full"):
            argv.append("--full")
        return argv
    if key == "aboutme":
        limit = params.get("limit", "50")
        argv = [py, str(SCRAPERS_DIR / "aboutme" / "aboutme_scrape.py"),
                "--limit", str(limit), "--cursor-out", str(ABOUTME_CURSOR)]
        cursor = (STATE.get("aboutme") or {}).get("cursor")
        if cursor:
            argv += ["--start-sitemap", cursor]
        return argv
    raise ValueError(f"unknown source: {key}")


def _terminate(proc):
    """Best-effort terminate a subprocess (terminate, then kill on grace timeout)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _run_scrape(key: str, params: dict):
    """Run a scraper subprocess, stream its log, live-reload while it runs, and
    hot-reload once more when it finishes (or is stopped)."""
    job = JOBS[key]
    with DATA_LOCK:
        before = len(BY_SOURCE.get(key, []))
    try:
        argv = _scrape_argv(key, params)
    except ValueError as e:
        job.update(status="error", finished_at=_now_iso(), message=str(e))
        return

    job["log"].append(f"$ {' '.join(argv)}")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
    except OSError as e:
        job.update(status="error", finished_at=_now_iso(), message=f"failed to start: {e}")
        return

    job["proc"] = proc
    job["pid"] = proc.pid
    # If a stop landed between start_scrape() and Popen, honour it immediately.
    if job.get("stop_requested"):
        _terminate(proc)

    # Watchdog: re-read the source from disk every couple of seconds so the UI
    # sees new records stream in while the scraper is still running.
    stop_evt = threading.Event()

    def watch():
        while not stop_evt.wait(2.0):
            try:
                cnt = reload_source(key)
                job["added"] = cnt - before
                job["total"] = cnt
            except Exception:  # noqa: BLE001 -- never let the watchdog die
                pass

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            job["log"].append(line)
    rc = proc.wait()
    stop_evt.set()
    watcher.join(timeout=3)
    job["proc"] = None

    after = reload_source(key)
    added = after - before
    stopped = bool(job.get("stop_requested"))
    if stopped:
        status, message = "stopped", f"stopped · +{added} new ({after} total)"
    elif rc == 0:
        status, message = "done", f"+{added} new ({after} total)"
    else:
        status, message = "error", f"scraper exited with code {rc}"
    job.update(
        status=status,
        finished_at=_now_iso(),
        returncode=rc,
        added=added,
        total=after,
        message=message,
    )

    cursor = None
    cursor_file = CURSOR_FILES.get(key)
    if cursor_file and cursor_file.exists():
        try:
            cursor = cursor_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            cursor = None
    with STATE_LOCK:
        prev = STATE.get(key) or {}
        STATE[key] = {
            "last_run": job["finished_at"],
            "last_status": job["status"],
            "added": added,
            "total": after,
            "cursor": cursor or prev.get("cursor"),
        }
    save_state()


def start_scrape(key: str, params: dict) -> dict:
    """Start a background scrape for `key` unless one is already running."""
    if key not in SOURCE_BY_KEY:
        return {"ok": False, "error": f"unknown source '{key}' (pick a specific site)"}
    with JOBS_LOCK:
        cur = JOBS.get(key)
        if cur and cur.get("status") == "running":
            return {"ok": False, "error": "already running", "job": _job_view(key)}
        JOBS[key] = {
            "source": key,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "added": None,
            "total": None,
            "returncode": None,
            "message": "",
            "pid": None,
            "proc": None,
            "stop_requested": False,
            "log": deque(maxlen=300),
        }
    t = threading.Thread(target=_run_scrape, args=(key, params), daemon=True)
    t.start()
    return {"ok": True, "job": _job_view(key)}


def stop_scrape(key: str) -> dict:
    """Request a running scrape to stop. Partial progress already on disk is kept."""
    if key not in SOURCE_BY_KEY:
        return {"ok": False, "error": f"unknown source '{key}'"}
    with JOBS_LOCK:
        job = JOBS.get(key)
        if not job or job.get("status") != "running":
            return {"ok": False, "error": "not running"}
        job["stop_requested"] = True
        proc = job.get("proc")
    _terminate(proc)  # if proc is None (not spawned yet), _run_scrape stops it on start
    return {"ok": True, "job": _job_view(key)}


def _job_view(key: str) -> dict:
    """Serialisable snapshot of a job + its persisted state."""
    job = JOBS.get(key)
    state = STATE.get(key) or {}
    view = {
        "source": key,
        "status": (job or {}).get("status", "idle"),
        "last_run": state.get("last_run"),
        "last_status": state.get("last_status"),
        "last_added": state.get("added"),
        "total": state.get("total"),
    }
    if job:
        view.update({
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "added": job.get("added"),
            "message": job.get("message"),
            "log": list(job.get("log", []))[-30:],
        })
    return view


# ---- query ----------------------------------------------------------------
def query_records(source: str, q: str, sort: str, page: int, per_page: int):
    with DATA_LOCK:
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
    server_version = "ContactDirectory/3.0"

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
        params = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if route == "/api/stats":
            source = params.get("source", ["all"])[0]
            with DATA_LOCK:
                self._send_json({
                    "stats": STATS_BY_SOURCE.get(source, STATS_BY_SOURCE["all"]),
                    "sources": SOURCE_LIST,
                })
            return

        if route == "/api/emails":
            page = _to_int(params.get("page", ["1"])[0], 1)
            per_page = _to_int(params.get("per_page", ["12"])[0], 12)
            per_page = max(1, min(per_page, MAX_PER_PAGE))
            q = params.get("q", [""])[0]
            sort = params.get("sort", ["newest"])[0]
            source = params.get("source", ["all"])[0]
            self._send_json(query_records(source, q, sort, page, per_page))
            return

        if route == "/api/scrape/status":
            source = params.get("source", [""])[0]
            if source:
                self._send_json(_job_view(source))
            else:
                self._send_json({"jobs": [_job_view(s["key"]) for s in SOURCES]})
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        source = params.get("source", [""])[0]

        if parsed.path == "/api/scrape/stop":
            result = stop_scrape(source)
            self._send_json(result, status=200 if result.get("ok") else 409)
            return

        if parsed.path == "/api/scrape":
            scrape_params = {}
            for k in ("pages", "limit"):
                if k in params:
                    scrape_params[k] = params[k][0]
            if params.get("full", ["0"])[0] in ("1", "true", "yes"):
                scrape_params["full"] = True
            result = start_scrape(source, scrape_params)
            self._send_json(result, status=200 if result.get("ok") else 409)
            return

        self._send_json({"error": "not found"}, 404)


def _to_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main():
    port = _to_int(sys.argv[1], 8000) if len(sys.argv) > 1 else 8000
    load_state()
    load_all()
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
