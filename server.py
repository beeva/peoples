#!/usr/bin/env python3
"""Lightweight zero-dependency JSON API for browsing scraped contacts.

This is the data API consumed by the Next.js app in web/ (run it separately
with `npm run dev`). It exposes pagination, full-text search, sorting,
**multiple data sources**, single-contact detail, Claude-generated outreach
email, and **on-demand incremental re-scraping** of each source:

    * discourse  -> scrapers/discourse/threejs/threejs_emails.jsonl
    * devto      -> scrapers/devto/jobs.json
    * aboutme    -> scrapers/aboutme/users.jsonl

Each source is normalised into one common record shape (with a `source` tag).
A re-scrape runs the source's scraper as a background subprocess; every scraper
is incremental (it skips content it already has), so re-scraping only fetches
what is new. After a scrape finishes the source is hot-reloaded in memory.

No third-party packages required. Configuration (API key, SMTP, enrichment)
is read from a `.env` file next to this script if present -- see `.env.example`:

    python server.py            # http://127.0.0.1:8000
    python server.py 9000       # custom port
"""
import json
import os
import re
import smtplib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
SCRAPERS_DIR = BASE_DIR / "scrapers"
STATE_FILE = SCRAPERS_DIR / "state.json"
MAX_PER_PAGE = 100


def _load_dotenv(path: Path) -> None:
    """Minimal `.env` loader (stdlib only): KEY=VALUE per line.

    Lines may be blank, `# comments`, or `export KEY=VALUE`. Surrounding quotes
    are stripped. A real environment variable always wins over the file, so the
    `.env` is just convenient defaults you can override at the shell.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


# Load .env before reading any config below (real env vars take precedence).
_load_dotenv(BASE_DIR / ".env")

# ---- outbound messaging (Claude-generated email) --------------------------
# Everything sensitive comes from the environment -- nothing is hardcoded.
# Required to generate:  ANTHROPIC_API_KEY
# Required to send:      SMTP_PASSWORD  (an Outlook app password)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
# Display name shown to the recipient (and used for the email sign-off).
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Ephrem")

# ---- contact enrichment (Claude-inferred country + gender, cached) --------
# Inferred once per contact (keyed by primary email) and cached to disk, then
# filled in lazily by background workers for contacts that get viewed.
ENRICH_FILE = SCRAPERS_DIR / "enrich_cache.json"
ENRICH_ENABLED = bool(ANTHROPIC_API_KEY) and \
    os.environ.get("ENRICH", "1").lower() not in ("0", "false", "no")
ENRICH_WORKERS = max(1, int(os.environ.get("ENRICH_WORKERS", "3")))
ENRICH_LOCK = threading.Lock()
ENRICH_CACHE: dict = {}      # primary email -> {"country","country_code","gender"}
ENRICH_QUEUE: deque = deque()
ENRICH_SEEN: set = set()     # emails queued/done this run (avoid re-enqueue)

# ---- sent-message log (which contacts we've emailed) ----------------------
# Persisted to disk so "Sent" badges survive restarts. Keyed by recipient
# email (lowercased); a contact counts as messaged if ANY of its emails match.
SENT_FILE = SCRAPERS_DIR / "sent.json"
SENT_LOCK = threading.Lock()
SENT_LOG: dict = {}          # recipient email -> {"count","last_sent","last_subject"}

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


def _md_clean(markdown: str) -> str:
    """Full markdown -> plain text, untruncated (for the detail page)."""
    return _strip_html(_MD_RE.sub(" ", markdown or ""))


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
            search_extra="", full=""):
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
    # Full, untruncated cleaned text of this single occurrence -- shown on the
    # detail page (the list only needs `preview`). Falls back to the preview.
    rec["_full"] = (full or rec["preview"] or "").strip()
    rec["_ts"] = _parse_ts(created_at or "")
    rec["_blob"] = " ".join([
        " ".join(uniq), rec["name"], rec["username"], rec["title"],
        rec["location"], rec["organization"], " ".join(rec["tags"]),
        rec["_full"], search_extra,
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
            full=_strip_html(d.get("cooked", "")),
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
            full=_md_clean(d.get("description", "")),
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
            full=_strip_html(d.get("summary") or ""),
            location=d.get("location") or "",
            links=_norm_links(d.get("links")),
            search_extra=" ".join(d.get("schools", []) + d.get("interests", [])),
        ))
    return records


# ---- merge same-email rows ------------------------------------------------
def _merge_group(group: list[dict]) -> dict:
    """Collapse records that belong to one contact into a single row.

    The newest record is the representative (its title/url/preview head the
    card); every other occurrence is kept under ``posts`` so nothing is lost.
    List fields (emails, tags, links) are unioned, order-preserving.
    """
    group = sorted(group, key=lambda r: r["_ts"], reverse=True)
    rep = dict(group[0])

    def first(field: str) -> str:
        for r in group:
            if r.get(field):
                return r[field]
        return rep.get(field, "")

    def union(field: str) -> list:
        seen, out = set(), []
        for r in group:
            for v in r.get(field) or []:
                if v not in seen:
                    seen.add(v)
                    out.append(v)
        return out

    rep["emails"] = union("emails")
    rep["name"] = first("name")
    rep["username"] = first("username")
    rep["organization"] = first("organization")
    rep["location"] = first("location")
    rep["tags"] = union("tags")
    rep["apply_links"] = union("apply_links")
    rep["messaging"] = union("messaging")
    rep["links"] = union("links")
    rep["post_count"] = len(group)
    # Compact refs to a few other occurrences (besides the representative) -- a
    # light teaser for the list card. Capped so the list payload stays small.
    rep["posts"] = [
        {"title": r.get("title") or "", "url": r.get("url") or "",
         "created_at": r.get("created_at") or ""}
        for r in group[1:6] if (r.get("url") or r.get("title"))
    ]
    # Every occurrence with its FULL text -- served only by the detail endpoint.
    rep["_detail_posts"] = [
        {"title": r.get("title") or "", "url": r.get("url") or "",
         "created_at": r.get("created_at") or "", "text": r.get("_full") or ""}
        for r in group
    ]
    rep["_occurrences"] = len(group)
    rep["_ts"] = max(r["_ts"] for r in group)
    rep["_blob"] = " ".join(r["_blob"] for r in group)
    # Inferred country/gender (filled from cache; empty until enriched).
    enr = _enrichment_for(rep["emails"][0] if rep["emails"] else "")
    rep["country"] = enr.get("country", "")
    rep["country_code"] = enr.get("country_code", "")
    rep["gender"] = enr.get("gender", "")
    # Have we emailed this contact? (filled from the persisted sent log.)
    _set_sent_fields(rep)
    return rep


def _merge_by_email(records: list[dict]) -> list[dict]:
    """Group a source's records so each contact email appears on one row.

    Records are linked when they share any email (union-find), so a person who
    posted the same address across many posts collapses to a single row even if
    some posts list extra addresses. Records with no email stay on their own.
    """
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def link(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    seen_email: dict[str, int] = {}
    for idx, rec in enumerate(records):
        for e in rec["emails"]:
            if e in seen_email:
                link(seen_email[e], idx)
            else:
                seen_email[e] = idx

    groups: dict[int, list[dict]] = {}
    for idx in range(len(records)):
        groups.setdefault(find(idx), []).append(records[idx])
    return [_merge_group(g) for g in groups.values()]


# ---- source registry ------------------------------------------------------
SOURCES = [
    {"key": "discourse", "label": "three.js forum", "noun": "Posts", "loader": load_discourse},
    {"key": "aboutme", "label": "about.me", "noun": "Profiles", "loader": load_aboutme},
]
SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}
NOUNS = {s["key"]: s["noun"] for s in SOURCES}
NOUNS["all"] = "Records"

PUBLIC_FIELDS = (
    "id", "source", "emails", "name", "username", "title", "url",
    "created_at", "preview", "tags", "location", "organization",
    "apply_links", "messaging", "links", "posts", "post_count",
    "country", "country_code", "gender",
    "messaged", "messaged_count", "messaged_at", "messaged_to",
)

# In-memory dataset, guarded by DATA_LOCK (ThreadingHTTPServer is multi-threaded).
DATA_LOCK = threading.RLock()
BY_SOURCE = {}
ALL_RECORDS = []
RECORD_BY_ID = {}
STATS_BY_SOURCE = {}
SOURCE_LIST = []


def _stats_for(records, noun):
    ts_values = [r["_ts"] for r in records if r["_ts"]]
    unique = {e for r in records for e in r["emails"]}
    return {
        "total_posts": sum(r.get("_occurrences", 1) for r in records),
        "total_emails": sum(len(r["emails"]) for r in records),
        "unique_emails": len(unique),
        "earliest": _fmt_date(min(ts_values)) if ts_values else None,
        "latest": _fmt_date(max(ts_values)) if ts_values else None,
        "noun": noun,
    }


def _rebuild_aggregates():
    """Recompute ALL_RECORDS / stats / source list from BY_SOURCE. Hold DATA_LOCK."""
    global ALL_RECORDS, RECORD_BY_ID, STATS_BY_SOURCE, SOURCE_LIST
    ALL_RECORDS = [r for s in SOURCES for r in BY_SOURCE.get(s["key"], [])]
    RECORD_BY_ID = {r["id"]: r for r in ALL_RECORDS}
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
        BY_SOURCE[key] = _merge_by_email(SOURCE_BY_KEY[key]["loader"]())
        _rebuild_aggregates()
        return len(BY_SOURCE[key])


def load_all():
    with DATA_LOCK:
        for s in SOURCES:
            BY_SOURCE[s["key"]] = _merge_by_email(s["loader"]())
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
def detail_record(rec_id: str) -> dict | None:
    """Full view of one merged contact: public fields + every post's full text."""
    with DATA_LOCK:
        rec = RECORD_BY_ID.get(rec_id)
        if rec is None:
            return None
        _enqueue_enrichment(rec)
        view = {k: rec.get(k) for k in PUBLIC_FIELDS}
        view["posts_full"] = rec.get("_detail_posts", [])
        return view


# ---- Claude helpers (structured JSON via the Messages API) -----------------
def _claude_json(system: str, user: str, schema: dict, max_tokens: int = 1024) -> dict:
    """One Claude call constrained to `schema` via output_config.format.

    Returns the parsed JSON object. Raises RuntimeError on transport/API errors
    and json.JSONDecodeError if the (guaranteed-JSON) text somehow won't parse.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server")
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Claude API error {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach Claude API: {e.reason}")
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined the request")
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    return json.loads(text)


# ---- Claude-generated outreach + email sending ----------------------------
GEN_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def _contact_context(rec: dict) -> str:
    """A compact, factual brief about the contact for the prompt (their posts)."""
    lines = []
    for p in (rec.get("_detail_posts") or [])[:5]:
        title = (p.get("title") or "").strip()
        snippet = " ".join((p.get("text") or "").split())[:400]
        if title or snippet:
            lines.append(f"- {title}: {snippet}".strip(" -:"))
    return "\n".join(lines) or "(no posts or profile text captured)"


def generate_message(rec: dict, intent: str, tone: str) -> dict:
    """Ask Claude for a personalised {subject, body}. Raises on any failure."""
    name = rec.get("name") or rec.get("username") or "there"
    system = (
        "You write short, personalised cold-outreach emails. The body must be "
        "plain text (no markdown), 80-150 words, with a greeting that uses the "
        "recipient's name and a brief sign-off using the sender's name. "
        "Reference the recipient's own work when it is relevant; never invent "
        "facts about them."
    )
    user = (
        f"Recipient name: {name}\n"
        f"Sender name (sign the email off as this): {MAIL_FROM_NAME}\n"
        f"Source: {rec.get('source')}\n"
        f"Context from their posts/profile:\n{_contact_context(rec)}\n\n"
        f"Desired tone: {tone or 'friendly and professional'}\n"
        f"Goal of the email / what to say:\n"
        f"{intent or 'A brief, warm introduction and an invitation to connect.'}"
    )
    try:
        obj = _claude_json(system, user, GEN_SCHEMA, max_tokens=1024)
    except json.JSONDecodeError:
        raise RuntimeError("Claude returned malformed output")
    return {"subject": (obj.get("subject") or "").strip(),
            "body": (obj.get("body") or "").strip()}


def send_email(to_addr: str, subject: str, body: str) -> None:
    """Send a plain-text email from MAIL_FROM via Outlook SMTP. Raises on failure."""
    if not SMTP_PASSWORD:
        raise RuntimeError("SMTP_PASSWORD is not set on the server (Outlook app password)")
    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM)) if MAIL_FROM_NAME else MAIL_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body or "")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        err = e.smtp_error
        detail = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        hint = ("SMTP login failed. The provider rejected authentication. ")
        if "5.7.139" in detail or "SmtpClientAuthentication is disabled" in detail:
            hint = ("SMTP login failed: SMTP AUTH is disabled for this mailbox, so "
                    "no password will work over SMTP. Enable 'Authenticated SMTP' "
                    "(Microsoft 365 admin), or switch SMTP_HOST/USER/PASSWORD to a "
                    "provider that allows app-password SMTP (e.g. Gmail). ")
        raise RuntimeError(hint + (f"(server said: {e.smtp_code} {detail})" if detail else ""))
    except (smtplib.SMTPException, OSError) as e:
        raise RuntimeError(f"could not send email: {e}")


# ---- contact enrichment: infer country + gender, cached + background-filled
ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "country": {"type": "string"},
        "country_code": {"type": "string"},
        "gender": {"type": "string", "enum": ["male", "female", "unknown"]},
    },
    "required": ["country", "country_code", "gender"],
    "additionalProperties": False,
}


def _infer_country_gender(item: dict) -> dict:
    """Ask Claude to infer country + gender from a contact's sparse signals."""
    system = (
        "You infer a person's likely country and gender from sparse signals: "
        "their name, any stated location, and text they wrote. Reply with JSON. "
        "country = English country name or 'Unknown'. country_code = its ISO "
        "3166-1 alpha-2 code (uppercase) or '' if unknown. gender = 'male', "
        "'female', or 'unknown'. A clearly gendered given name or an explicit "
        "location is enough; when signals are weak prefer Unknown over guessing."
    )
    user = (
        f"Name: {item.get('name') or '(unknown)'}\n"
        f"Stated location: {item.get('location') or '(none)'}\n"
        f"Text they wrote:\n{(item.get('context') or '')[:800]}"
    )
    obj = _claude_json(system, user, ENRICH_SCHEMA, max_tokens=200)
    gender = str(obj.get("gender") or "unknown").lower()
    if gender not in ("male", "female", "unknown"):
        gender = "unknown"
    return {
        "country": (obj.get("country") or "").strip(),
        "country_code": (obj.get("country_code") or "").strip().upper()[:2],
        "gender": gender,
    }


def _load_enrich_cache() -> None:
    global ENRICH_CACHE
    try:
        data = json.loads(ENRICH_FILE.read_text(encoding="utf-8"))
        ENRICH_CACHE = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        ENRICH_CACHE = {}


def _save_enrich_cache() -> None:
    try:
        with ENRICH_LOCK:
            snapshot = dict(ENRICH_CACHE)
        ENRICH_FILE.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"[warn] could not write {ENRICH_FILE.name}: {e}", file=sys.stderr)


def _enrichment_for(email: str) -> dict:
    if not email:
        return {}
    with ENRICH_LOCK:
        return ENRICH_CACHE.get(email) or {}


def _enqueue_enrichment(rec: dict) -> None:
    """Queue a contact for background country/gender inference (once per email)."""
    if not ENRICH_ENABLED:
        return
    emails = rec.get("emails") or []
    if not emails:
        return
    key = emails[0]
    with ENRICH_LOCK:
        if key in ENRICH_CACHE or key in ENRICH_SEEN:
            return
        ENRICH_SEEN.add(key)
        ENRICH_QUEUE.append({
            "email": key,
            "name": rec.get("name") or rec.get("username") or "",
            "location": rec.get("location") or "",
            "context": rec.get("preview") or "",
        })


def _apply_enrichment(email: str, enr: dict) -> None:
    """Write inferred fields onto every in-memory record for this contact."""
    with DATA_LOCK:
        for rec in ALL_RECORDS:
            ems = rec.get("emails") or []
            if ems and ems[0] == email:
                rec["country"] = enr.get("country", "")
                rec["country_code"] = enr.get("country_code", "")
                rec["gender"] = enr.get("gender", "")


def _enrich_worker() -> None:
    """Drain the queue, infer per contact, cache, and patch loaded records."""
    while True:
        try:
            item = ENRICH_QUEUE.popleft()
        except IndexError:
            time.sleep(0.5)
            continue
        email = item["email"]
        with ENRICH_LOCK:
            if email in ENRICH_CACHE:
                continue
        try:
            enr = _infer_country_gender(item)
        except Exception:  # noqa: BLE001 -- transient (rate limit/network); retry later
            with ENRICH_LOCK:
                ENRICH_SEEN.discard(email)  # allow a future re-enqueue
            time.sleep(1.5)
            continue
        with ENRICH_LOCK:
            ENRICH_CACHE[email] = enr
        _apply_enrichment(email, enr)
        _save_enrich_cache()
        time.sleep(0.2)  # gentle throttle


# ---- sent-message log: record sends, mark messaged contacts ---------------
def _load_sent_log() -> None:
    global SENT_LOG
    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        SENT_LOG = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        SENT_LOG = {}


def _save_sent_log() -> None:
    try:
        with SENT_LOCK:
            snapshot = dict(SENT_LOG)
        SENT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except OSError as e:
        print(f"[warn] could not write {SENT_FILE.name}: {e}", file=sys.stderr)


def _sent_for(emails) -> dict:
    """Aggregate send history across all of a contact's emails."""
    out = {"count": 0, "last_sent": "", "to": ""}
    with SENT_LOCK:
        for e in emails or []:
            ent = SENT_LOG.get(e)
            if not ent:
                continue
            out["count"] += ent.get("count", 0)
            if ent.get("last_sent", "") >= out["last_sent"]:
                out["last_sent"] = ent.get("last_sent", "")
                out["to"] = e
    return out


def _set_sent_fields(rec: dict) -> None:
    snt = _sent_for(rec.get("emails"))
    rec["messaged"] = snt["count"] > 0
    rec["messaged_count"] = snt["count"]
    rec["messaged_at"] = snt["last_sent"]
    rec["messaged_to"] = snt["to"]


def _apply_sent(email: str) -> None:
    """Refresh the messaged fields on every in-memory record holding this email."""
    with DATA_LOCK:
        for rec in ALL_RECORDS:
            if email in (rec.get("emails") or []):
                _set_sent_fields(rec)


def _record_sent(to_addr: str, subject: str) -> None:
    """Log a successful send and mark the matching contact(s) as messaged."""
    key = (to_addr or "").strip().lower()
    if not key:
        return
    with SENT_LOCK:
        ent = SENT_LOG.get(key) or {"count": 0}
        ent["count"] = ent.get("count", 0) + 1
        ent["last_sent"] = _now_iso()
        ent["last_subject"] = (subject or "").strip()
        SENT_LOG[key] = ent
    _save_sent_log()
    _apply_sent(key)


def query_records(source: str, q: str, sort: str, page: int, per_page: int,
                  messaged: str = "all"):
    with DATA_LOCK:
        items = BY_SOURCE.get(source) if source != "all" else ALL_RECORDS
        if items is None:  # unknown source -> behave like "all"
            source = "all"
            items = ALL_RECORDS

        q = (q or "").strip().lower()
        if q:
            terms = q.split()
            items = [r for r in items if all(t in r["_blob"] for t in terms)]

        # Counts for the sent/unsent filter, over the current source+search set.
        sent_n = sum(1 for r in items if r.get("messaged"))
        messaged_counts = {"all": len(items), "sent": sent_n,
                           "unsent": len(items) - sent_n}
        # `messaged` is a set of {sent, unsent} (comma-separated). Selecting both
        # (or neither) is the same as "all" -- no filtering.
        sel = sorted({m for m in (messaged or "").split(",")
                      if m in ("sent", "unsent")})
        if sel == ["sent"]:
            items = [r for r in items if r.get("messaged")]
        elif sel == ["unsent"]:
            items = [r for r in items if not r.get("messaged")]
        messaged = ",".join(sel) if sel else "all"

        reverse = sort != "oldest"
        items = sorted(items, key=lambda r: r["_ts"], reverse=reverse)

        total = len(items)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        window = items[start:start + per_page]
        for r in window:
            _enqueue_enrichment(r)
        payload = [{k: r.get(k) for k in PUBLIC_FIELDS} for r in window]

        return {
            "items": payload,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "source": source,
            "messaged": messaged,
            "messaged_counts": messaged_counts,
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

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send_json({
                "service": "email-scrapper data API",
                "ui": "Run the Next.js app in web/ (npm run dev); it consumes this API.",
                "endpoints": [
                    "/api/emails", "/api/email?id=", "/api/stats",
                    "/api/scrape", "/api/scrape/status", "/api/scrape/stop",
                    "/api/message/generate", "/api/message/send",
                ],
            })
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
            messaged = params.get("messaged", ["all"])[0]
            self._send_json(query_records(source, q, sort, page, per_page, messaged))
            return

        if route == "/api/email":
            rec_id = params.get("id", [""])[0]
            view = detail_record(rec_id)
            if view is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(view)
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

        if parsed.path == "/api/message/generate":
            data = self._read_json_body()
            with DATA_LOCK:
                rec = RECORD_BY_ID.get(data.get("id") or "")
            if rec is None:
                self._send_json({"ok": False, "error": "unknown contact id"}, 404)
                return
            try:
                result = generate_message(rec, data.get("intent", ""), data.get("tone", ""))
                self._send_json({"ok": True, **result})
            except Exception as e:  # noqa: BLE001 -- surface the reason to the UI
                self._send_json({"ok": False, "error": str(e)}, 502)
            return

        if parsed.path == "/api/message/send":
            data = self._read_json_body()
            to_addr = (data.get("to") or "").strip()
            if not to_addr and data.get("id"):
                with DATA_LOCK:
                    rec = RECORD_BY_ID.get(data["id"])
                if rec and rec.get("emails"):
                    to_addr = rec["emails"][0]
            if not to_addr:
                self._send_json({"ok": False, "error": "no recipient address"}, 400)
                return
            try:
                send_email(to_addr, data.get("subject", ""), data.get("body", ""))
                _record_sent(to_addr, data.get("subject", ""))
                self._send_json({"ok": True, "to": to_addr})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)}, 502)
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
    _load_enrich_cache()
    _load_sent_log()
    load_all()
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, Handler)
    counts = ", ".join(f"{s['key']}={len(BY_SOURCE[s['key']])}" for s in SOURCES)
    print(f"Loaded {len(ALL_RECORDS)} records ({counts})")
    msg_gen = "on" if ANTHROPIC_API_KEY else "OFF (set ANTHROPIC_API_KEY)"
    msg_send = "on" if SMTP_PASSWORD else "OFF (set SMTP_PASSWORD)"
    print(f"Messaging: generate={msg_gen}, send={msg_send}, from={MAIL_FROM} "
          f"({len(SENT_LOG)} recipients messaged)")
    if ENRICH_ENABLED:
        for _ in range(ENRICH_WORKERS):
            threading.Thread(target=_enrich_worker, daemon=True).start()
        print(f"Enrichment: on ({len(ENRICH_CACHE)} cached, {ENRICH_WORKERS} workers)")
    else:
        print("Enrichment: off (needs ANTHROPIC_API_KEY; ENRICH=0 to disable)")
    print(f"Serving on http://{addr[0]}:{addr[1]}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
