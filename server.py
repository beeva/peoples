#!/usr/bin/env python3
"""Lightweight zero-dependency JSON API for browsing scraped contacts.

This is the data API consumed by the Next.js app in web/ (run it separately
with `npm run dev`). It exposes pagination, full-text search, sorting,
**multiple data sources**, single-contact detail, Claude-generated outreach
email, and **on-demand incremental re-scraping** of each source:

    * discourse  -> scrapers/discourse/threejs/threejs_emails.jsonl
    * devto      -> scrapers/devto/jobs.json
    * aboutme    -> scrapers/aboutme/users.jsonl
    * github     -> scrapers/github/users.jsonl

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
import unicodedata
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

# The scrapers decide which of a person's addresses is the one to write to; the
# API reads that same rule rather than keeping a second opinion. Applying it on
# load also means records scraped before the rule existed are ordered correctly
# without re-scraping them.
sys.path.insert(0, str(SCRAPERS_DIR))
from common import personal_first  # noqa: E402
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
# Entries written by /api/message/mark carry "manual": true -- the contact was
# emailed outside this app, so there is no body/subject to remember.
SENT_FILE = SCRAPERS_DIR / "sent.json"
SENT_LOCK = threading.Lock()
SENT_LOG: dict = {}          # recipient email -> {"count","last_sent","last_subject"}

DISCOURSE_FILE = SCRAPERS_DIR / "discourse" / "threejs" / "threejs_emails.jsonl"
DEVTO_FILE = SCRAPERS_DIR / "devto" / "jobs.json"
ABOUTME_FILE = SCRAPERS_DIR / "aboutme" / "users.jsonl"
GITHUB_FILE = SCRAPERS_DIR / "github" / "users.jsonl"

# Per-source resume cursors written by the scrapers (git-ignored runtime state).
DISCOURSE_CURSOR = SCRAPERS_DIR / "discourse" / "threejs" / ".cursor"
ABOUTME_CURSOR = SCRAPERS_DIR / "aboutme" / ".cursor"
GITHUB_CURSOR = SCRAPERS_DIR / "github" / ".cursor"
CURSOR_FILES = {"discourse": DISCOURSE_CURSOR, "aboutme": ABOUTME_CURSOR,
                "github": GITHUB_CURSOR}

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
            search_extra="", full="", site_url="", activity_at="", run=0,
            country="", country_code="", gender=""):
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
        # The person's own site (a GitHub profile's `blog`), as opposed to the
        # profile page itself -- worth its own field so the UI can label it.
        "site_url": site_url or "",
        "created_at": created_at or "",
        # When the contact was last seen active at the source ("" if unknown).
        "activity_at": activity_at or "",
        # Which scrape run collected this contact (0 = the source does not
        # number its runs). Lets the UI show what a given run brought in.
        "run": run or 0,
        # Country/gender the *scraper* determined (GitHub: from location, and
        # gender only when a --gender scrape ran). A fallback for the merge when
        # Claude enrichment has not filled these in yet.
        "_scraped_country": (country or "").strip(),
        "_scraped_country_code": (country_code or "").strip().upper()[:2],
        "_scraped_gender": (gender or "").strip().lower(),
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
    # Sort on the date the UI shows: for a profile that is "last active", for a
    # post it is when it was written. Keeps the Date column and its sort honest.
    rec["_ts"] = _parse_ts(activity_at or created_at or "")
    # The join date on its own, for the account-age filter (0.0 = unknown).
    rec["_created_ts"] = _parse_ts(created_at or "")
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


def load_github():
    records = []
    for i, d in enumerate(_read_jsonl(GITHUB_FILE), 1):
        login = d.get("login") or str(d.get("user_id") or i)
        bio = d.get("bio") or ""
        # Which sources the email came from ("profile", "site", ...) -- worth
        # surfacing as tags: a profile email is a warmer lead than a commit one.
        via = list((d.get("email_sources") or {}).keys())
        twitter = d.get("twitter")
        # The GitHub profile is `url`; the portfolio site is its own field. Any
        # Twitter handle rounds out the "links" list shown on the detail card.
        links = [f"https://twitter.com/{twitter}"] if twitter else []
        records.append(_record(
            "github", login,
            # Personal mailbox first (gmail, proton, ...), then a work address,
            # then a role desk (support@, info@) -- emails[0] is what the table
            # shows and what the Message button writes to. Stable, so within a
            # rank the scraper's own source order (profile > site > commit) holds.
            emails=personal_first(d.get("emails", [])),
            name=d.get("full_name") or login,
            username=login,
            title=d.get("company") or "",
            url=d.get("url") or f"https://github.com/{login}",
            site_url=d.get("site_url") or "",
            created_at=d.get("created_at", ""),        # the day they joined
            activity_at=d.get("last_activity", ""),    # last public activity
            # Users scraped before runs were numbered are, by definition, run 1.
            run=_to_int(str(d.get("run", "")), 1),
            country=d.get("country") or "",            # from regions.py classify()
            country_code=d.get("country_code") or "",
            gender=d.get("gender") or "",              # set only by a --gender run
            preview=_preview(bio),
            full=bio,
            tags=[t for t in [d.get("country")] if t] + [f"via {v}" for v in via],
            location=d.get("location") or "",
            organization=d.get("company") or "",
            links=links,
            search_extra=" ".join(filter(None, (
                d.get("country"), d.get("region"), d.get("site_url"), twitter,
            ))),
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
    rep["site_url"] = first("site_url")
    # Latest activity across everything merged into this contact.
    rep["activity_at"] = max((r.get("activity_at") or "" for r in group),
                             default="")
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
    # Country/gender: Claude enrichment first, then whatever the scraper already
    # worked out (GitHub knows the country from the location, and the gender too
    # when a --gender scrape ran). So a scrape-time value shows at once and is
    # not re-inferred, while enrichment still fills the gaps for other sources.
    enr = _enrichment_for(rep["emails"][0] if rep["emails"] else "")
    rep["country"] = enr.get("country") or rep.get("_scraped_country") or ""
    rep["country_code"] = (enr.get("country_code")
                           or rep.get("_scraped_country_code") or "")
    rep["gender"] = enr.get("gender") or rep.get("_scraped_gender") or ""
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
    {"key": "github", "label": "GitHub", "noun": "Profiles", "loader": load_github},
]
SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}
NOUNS = {s["key"]: s["noun"] for s in SOURCES}
NOUNS["all"] = "Records"

PUBLIC_FIELDS = (
    "id", "source", "emails", "name", "username", "title", "url", "site_url",
    "created_at", "activity_at", "run", "preview", "tags", "location",
    "organization", "apply_links", "messaging", "links", "posts", "post_count",
    "country", "country_code", "gender",
    "messaged", "messaged_count", "messaged_at", "messaged_to", "messaged_manual",
)


# ---- sorting --------------------------------------------------------------
def _text_key(value: str) -> str:
    """Sort key for a text column: case-insensitive, accents folded.

    Without the folding, "Ángel" sorts after "Zoe" -- accented letters live far
    above Z in Unicode -- so an A-Z list would end on a name that starts with A.
    Folding puts it where a reader looks for it, next to "Andres".
    """
    decomposed = unicodedata.normalize("NFKD", (value or "").strip().lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _name_of(rec: dict) -> str:
    return _text_key(rec.get("name") or rec.get("username") or "")


def _email_of(rec: dict) -> str:
    emails = rec.get("emails") or []
    return _text_key(emails[0] if emails else "")


def _blank_last(key_fn):
    """Wrap a text key so empty values sink to the bottom whichever way we sort.

    With `reverse=True` Python flips the whole tuple, so a plain (blank, text)
    key would float the blanks to the top. Negating the flag for the descending
    variants keeps them where they belong.
    """
    def ascending(rec):
        value = key_fn(rec)
        return (value == "", value)

    def descending(rec):
        value = key_fn(rec)
        return (value != "", value)

    return ascending, descending


_name_asc, _name_desc = _blank_last(_name_of)
_email_asc, _email_desc = _blank_last(_email_of)
_country_asc, _country_desc = _blank_last(lambda r: _text_key(r.get("country") or ""))

# sort key -> (key function, reverse). The date sorts keep their old names, so
# links and bookmarks made before the other columns existed still work.
SORTS = {
    "newest": (lambda r: r["_ts"], True),
    "oldest": (lambda r: r["_ts"], False),
    # Within one run the newest contact leads, so a run reads like a batch.
    "run_desc": (lambda r: (r.get("run") or 0, r["_ts"]), True),
    "run_asc": (lambda r: (r.get("run") or 0, -r["_ts"]), False),
    "name_asc": (_name_asc, False),
    "name_desc": (_name_desc, True),
    "email_asc": (_email_asc, False),
    "email_desc": (_email_desc, True),
    "country_asc": (_country_asc, False),
    "country_desc": (_country_desc, True),
}
DEFAULT_SORT = "newest"

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
    if key == "github":
        argv = [py, str(SCRAPERS_DIR / "github" / "github_scrape.py"),
                "--cursor-out", str(GITHUB_CURSOR)]
        # A target is a total ("get me to 1000 users"), so it counts the rows
        # already on disk and the run keeps sweeping until it is met. Without
        # one, a click means "fetch a batch" and 50 is a sane batch.
        target = _to_int(str(params.get("target", "")), 0)
        if target > 0:
            argv += ["--target", str(target)]
        # A per-run cap on top of a target would defeat it -- the run would stop
        # at 50 kept and never reach 1000 -- so the two are not combined.
        limit = _to_int(str(params.get("limit", "")), 0 if target > 0 else 50)
        argv += ["--limit", str(limit if target <= 0 else 0)]
        regions = params.get("regions")
        if regions:
            argv += ["--regions", str(regions)]
        pages = _to_int(str(params.get("pages", "")), 0)
        if pages > 0:
            argv += ["--pages", str(pages)]
        # Narrowing filters, mirroring the UI facet controls: scrape only the
        # countries / account ages / gender being targeted. The scraper applies
        # country + age cheaply (off the profile) and gender via Claude.
        country = str(params.get("country", "")).strip()
        if country:
            argv += ["--countries", country]
        age_min = _to_float(str(params.get("age_min", "")))
        age_max = _to_float(str(params.get("age_max", "")))
        if age_min is not None:
            argv += ["--age-min", str(age_min)]
        if age_max is not None:
            argv += ["--age-max", str(age_max)]
        gender = str(params.get("gender", "")).strip().lower()
        # The UI gender facet is multi-select; the scraper targets one gender,
        # so only forward it when exactly one of male/female is requested.
        genders = [g for g in gender.split(",") if g in ("male", "female")]
        if len(genders) == 1:
            argv += ["--gender", genders[0]]
        # Joined / last-active calendar windows: the UI sends an op + a date;
        # the scraper takes them as --<field>-after / --<field>-before.
        for field in ("joined", "active"):
            op = str(params.get(f"{field}_op", "")).strip().lower()
            date = str(params.get(f"{field}_date", "")).strip()
            if op in ("after", "before") and date:
                argv += [f"--{field}-{op}", date]
        # Explore a fresh, random slice of the world on every click. The walk
        # order is otherwise fixed, so a plain restart re-issues the same first
        # queries -- whose users are already on disk and get skipped -- and
        # wastes the run re-covering old ground. Shuffling heads somewhere new
        # each time; already-scraped users are still de-duped by `done`, so
        # nothing is collected twice. `shuffle=0` opts back into cursor resume.
        if str(params.get("shuffle", "1")) not in ("0", "false", "no"):
            argv.append("--shuffle")
        else:
            cursor = (STATE.get("github") or {}).get("cursor")
            if cursor:
                argv += ["--start-location", cursor]
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
    out = {"count": 0, "last_sent": "", "to": "", "manual": False}
    with SENT_LOCK:
        for e in emails or []:
            ent = SENT_LOG.get(e)
            if not ent:
                continue
            out["count"] += ent.get("count", 0)
            if ent.get("last_sent", "") >= out["last_sent"]:
                out["last_sent"] = ent.get("last_sent", "")
                out["to"] = e
                out["manual"] = bool(ent.get("manual"))
    return out


def _set_sent_fields(rec: dict) -> None:
    snt = _sent_for(rec.get("emails"))
    rec["messaged"] = snt["count"] > 0
    rec["messaged_count"] = snt["count"]
    rec["messaged_at"] = snt["last_sent"]
    rec["messaged_to"] = snt["to"]
    rec["messaged_manual"] = snt["manual"]


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


def _mark_sent(rec_id: str, sent: bool) -> dict | None:
    """Flip a contact's sent flag by hand, for emails sent outside this app.

    Marking sent logs the primary address; marking unsent forgets every address
    of the contact, including real sends -- that is what "unsent" has to mean
    for the badge and the sent/unsent filter to agree with each other.
    """
    with DATA_LOCK:
        rec = RECORD_BY_ID.get(rec_id or "")
    if rec is None:
        return None
    emails = [e.strip().lower() for e in (rec.get("emails") or []) if e.strip()]
    if not emails:
        return None

    with SENT_LOCK:
        if sent:
            ent = SENT_LOG.get(emails[0]) or {}
            if ent.get("count", 0) <= 0:
                ent = {"count": 1, "last_sent": _now_iso(), "last_subject": "",
                       "manual": True}
            SENT_LOG[emails[0]] = ent
        else:
            for e in emails:
                SENT_LOG.pop(e, None)
    _save_sent_log()

    with DATA_LOCK:
        _set_sent_fields(rec)
    for e in emails:
        _apply_sent(e)  # other records may share one of these addresses
    return {k: rec.get(k) for k in
            ("id", "messaged", "messaged_count", "messaged_at", "messaged_to",
             "messaged_manual")}


def _mark_sent_many(ids: list[str], sent: bool) -> int:
    """Bulk _mark_sent (the export dialog's "mark all as sent"): same rules,
    but one log write and one refresh pass instead of one per contact."""
    with DATA_LOCK:
        recs = [r for r in (RECORD_BY_ID.get(i or "") for i in ids)
                if r and r.get("emails")]
    touched: set[str] = set()
    with SENT_LOCK:
        for rec in recs:
            emails = [e.strip().lower() for e in rec["emails"] if e.strip()]
            if not emails:
                continue
            if sent:
                ent = SENT_LOG.get(emails[0]) or {}
                if ent.get("count", 0) <= 0:
                    SENT_LOG[emails[0]] = {"count": 1, "last_sent": _now_iso(),
                                           "last_subject": "", "manual": True}
            else:
                for e in emails:
                    SENT_LOG.pop(e, None)
            touched.update(emails)
    _save_sent_log()
    with DATA_LOCK:
        for rec in ALL_RECORDS:
            if touched.intersection(rec.get("emails") or ()):
                _set_sent_fields(rec)
    return len(recs)


_SECONDS_PER_YEAR = 365.25 * 86400


def _age_years(rec: dict, now_ts: float) -> float | None:
    """Account age in years from the join date, or None if it is unknown."""
    created = rec.get("_created_ts") or 0.0
    if created <= 0:
        return None
    return max(0.0, (now_ts - created) / _SECONDS_PER_YEAR)


def _filter_by_date(items: list[dict], op: str, date: str, ts_of) -> list[dict]:
    """Keep items whose date is after/before a calendar day.

    ``after`` is inclusive, ``before`` exclusive; a record missing the date in
    question cannot satisfy a condition on it, so it drops out. Shared by the
    list query and the export dialog so both mean the same thing by "after".
    """
    cutoff = _parse_ts(date) if date else 0.0
    if op not in ("after", "before") or not cutoff:
        return items
    if op == "after":
        return [r for r in items if ts_of(r) >= cutoff]
    return [r for r in items if 0.0 < ts_of(r) < cutoff]


def _facets(items: list[dict], now_ts: float) -> dict:
    """Counts per country, gender and age band over the given set.

    These describe the set the user is narrowing (source + search + sent), and
    are computed *before* the country/gender/age filters so the numbers hold
    steady as those are toggled -- a facet shows how many a choice would match,
    which is the point of showing the count next to it.
    """
    countries: dict[str, dict] = {}
    genders = {"male": 0, "female": 0, "unknown": 0}
    ages = {k: 0 for k in ("lt1", "1to3", "3to5", "5to10", "gte10", "unknown")}
    runs: dict[int, int] = {}
    for rec in items:
        name = (rec.get("country") or "").strip()
        if name and name.lower() != "unknown":
            entry = countries.setdefault(
                name, {"name": name, "code": rec.get("country_code") or "", "count": 0})
            entry["count"] += 1
        g = (rec.get("gender") or "").strip().lower()
        genders[g if g in genders else "unknown"] += 1
        # Step = scrape run number. Run 0 means "the source doesn't number
        # its runs", which is not a step anyone can meaningfully pick.
        run = rec.get("run") or 0
        if run > 0:
            runs[run] = runs.get(run, 0) + 1
        age = _age_years(rec, now_ts)
        if age is None:
            ages["unknown"] += 1
        elif age < 1:
            ages["lt1"] += 1
        elif age < 3:
            ages["1to3"] += 1
        elif age < 5:
            ages["3to5"] += 1
        elif age < 10:
            ages["5to10"] += 1
        else:
            ages["gte10"] += 1
    country_list = sorted(countries.values(),
                          key=lambda c: (-c["count"], c["name"].lower()))
    run_list = [{"run": k, "count": v} for k, v in sorted(runs.items())]
    return {"countries": country_list, "genders": genders, "ages": ages,
            "runs": run_list}


def query_records(source: str, q: str, sort: str, page: int, per_page: int,
                  messaged: str = "all", country: str = "", gender: str = "",
                  age_min: str = "", age_max: str = "", runs: str = "",
                  joined_op: str = "", joined_date: str = "",
                  active_op: str = "", active_date: str = ""):
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

        # Facets describe this set (before country/gender/age narrow it).
        now_ts = datetime.now(timezone.utc).timestamp()
        facets = _facets(items, now_ts)

        # Country: comma-separated names OR codes, case-insensitive, any-of.
        wanted_countries = {c.strip().lower() for c in (country or "").split(",")
                            if c.strip()}
        if wanted_countries:
            items = [r for r in items
                     if (r.get("country") or "").strip().lower() in wanted_countries
                     or (r.get("country_code") or "").strip().lower() in wanted_countries]

        # Gender: any-of male/female/unknown.
        wanted_genders = {g.strip().lower() for g in (gender or "").split(",")
                          if g.strip() in ("male", "female", "unknown")}
        if wanted_genders:
            items = [r for r in items
                     if ((r.get("gender") or "").strip().lower() or "unknown")
                     in wanted_genders]

        # Step (scrape run number): comma-separated, any-of.
        wanted_runs = {_to_int(x.strip(), -1)
                       for x in (runs or "").split(",") if x.strip()}
        wanted_runs.discard(-1)
        if wanted_runs:
            items = [r for r in items if (r.get("run") or 0) in wanted_runs]

        # Joined / last-active calendar windows.
        items = _filter_by_date(items, joined_op, joined_date,
                                lambda r: r.get("_created_ts") or 0.0)
        items = _filter_by_date(items, active_op, active_date,
                                lambda r: _parse_ts(r.get("activity_at") or ""))

        # Account age in years, min inclusive and max EXCLUSIVE -- so a one-year
        # band [N, N+1) means "N years old", and the bands tile without overlap
        # (they match the facet counts). A record whose join date is unknown
        # cannot satisfy an age bound, so it drops out.
        lo = _to_float(age_min)
        hi = _to_float(age_max)
        age_min = "" if lo is None else str(lo)
        age_max = "" if hi is None else str(hi)
        if lo is not None or hi is not None:
            kept = []
            for r in items:
                age = _age_years(r, now_ts)
                if age is None:
                    continue
                if lo is not None and age < lo:
                    continue
                if hi is not None and age >= hi:
                    continue
                kept.append(r)
            items = kept

        key_fn, reverse = SORTS.get(sort) or SORTS[DEFAULT_SORT]
        if sort not in SORTS:
            sort = DEFAULT_SORT
        items = sorted(items, key=key_fn, reverse=reverse)

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
            "country": ",".join(sorted(wanted_countries)),
            "gender": ",".join(sorted(wanted_genders)),
            "runs": ",".join(str(r) for r in sorted(wanted_runs)),
            "age_min": age_min,
            "age_max": age_max,
            "facets": facets,
            "stats": STATS_BY_SOURCE.get(source, STATS_BY_SOURCE["all"]),
            "sources": SOURCE_LIST,
        }


def export_records(source: str, gender: str = "", active_op: str = "",
                   active_date: str = "", joined_op: str = "",
                   joined_date: str = "", runs: str = "", country: str = "",
                   messaged: str = "", limit: int = 0):
    """Rows for the CSV export dialog: filtered, sorted, capped.

    Also returns the option lists (scrape runs, countries) the dialog builds
    its controls from -- computed over the WHOLE source set, so choices don't
    vanish from the dialog as the selection narrows. Unlike /api/emails this
    is not paginated: the preview table IS the file that gets downloaded, so
    the response must hold every row (bounded by ``limit``).
    """
    with DATA_LOCK:
        items = BY_SOURCE.get(source) or []
        # An export of "main emails" has no use for a row without one.
        items = [r for r in items if r.get("emails")]

        run_counts: dict[int, int] = {}
        country_counts: dict[str, dict] = {}
        for r in items:
            run = r.get("run") or 0
            run_counts[run] = run_counts.get(run, 0) + 1
            name = (r.get("country") or "").strip()
            if name:
                ent = country_counts.setdefault(
                    name, {"name": name, "code": "", "count": 0})
                ent["count"] += 1
                ent["code"] = ent["code"] or (r.get("country_code") or "").strip()
        options = {
            "runs": [{"run": k, "count": v}
                     for k, v in sorted(run_counts.items())],
            "countries": sorted(country_counts.values(),
                                key=lambda c: (-c["count"], c["name"].lower())),
        }

        # Sent/unsent: same contract as /api/emails -- picking both (or
        # neither) means "all".
        sel = sorted({m for m in (messaged or "").split(",")
                      if m in ("sent", "unsent")})
        if sel == ["sent"]:
            items = [r for r in items if r.get("messaged")]
        elif sel == ["unsent"]:
            items = [r for r in items if not r.get("messaged")]

        # Gender: any-of male/female/unknown (same contract as /api/emails).
        wanted_genders = {g.strip().lower() for g in (gender or "").split(",")
                          if g.strip() in ("male", "female", "unknown")}
        if wanted_genders:
            items = [r for r in items
                     if ((r.get("gender") or "").strip().lower() or "unknown")
                     in wanted_genders]

        # Country: comma-separated names OR codes, case-insensitive, any-of.
        wanted_countries = {c.strip().lower() for c in (country or "").split(",")
                            if c.strip()}
        if wanted_countries:
            items = [r for r in items
                     if (r.get("country") or "").strip().lower() in wanted_countries
                     or (r.get("country_code") or "").strip().lower() in wanted_countries]

        # Date conditions: after/before a calendar day (shared helper, same
        # inclusive-after / exclusive-before contract as the list filter).
        items = _filter_by_date(items, active_op, active_date,
                                lambda r: _parse_ts(r.get("activity_at") or ""))
        items = _filter_by_date(items, joined_op, joined_date,
                                lambda r: r.get("_created_ts") or 0.0)

        # Step = scrape run number, any-of.
        wanted_runs = {_to_int(x.strip(), -1)
                       for x in (runs or "").split(",") if x.strip()}
        wanted_runs.discard(-1)
        if wanted_runs:
            items = [r for r in items if (r.get("run") or 0) in wanted_runs]

        matched = len(items)
        items = sorted(items, key=lambda r: r["_ts"], reverse=True)
        if limit > 0:
            items = items[:limit]

        # Slim rows: exactly what the preview table and the CSV need. Only the
        # main email (emails[0], personal-first) is exported -- by design.
        rows = [{
            "id": r["id"],
            "name": r.get("name") or "",
            "username": r.get("username") or "",
            "email": r["emails"][0],
            "gender": r.get("gender") or "",
            "country": r.get("country") or "",
            "run": r.get("run") or 0,
            "created_at": r.get("created_at") or "",
            "activity_at": r.get("activity_at") or "",
            "messaged": bool(r.get("messaged")),
        } for r in items]

        return {"items": rows, "matched": matched, "options": options}


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
                    "/api/emails", "/api/email?id=", "/api/export", "/api/stats",
                    "/api/scrape", "/api/scrape/status", "/api/scrape/stop",
                    "/api/message/generate", "/api/message/send",
                    "/api/message/mark",
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
            self._send_json(query_records(
                source, q, sort, page, per_page, messaged,
                country=params.get("country", [""])[0],
                gender=params.get("gender", [""])[0],
                age_min=params.get("age_min", [""])[0],
                age_max=params.get("age_max", [""])[0],
                runs=params.get("runs", [""])[0],
                joined_op=params.get("joined_op", [""])[0],
                joined_date=params.get("joined_date", [""])[0],
                active_op=params.get("active_op", [""])[0],
                active_date=params.get("active_date", [""])[0],
            ))
            return

        if route == "/api/export":
            self._send_json(export_records(
                params.get("source", ["github"])[0],
                gender=params.get("gender", [""])[0],
                active_op=params.get("active_op", [""])[0],
                active_date=params.get("active_date", [""])[0],
                joined_op=params.get("joined_op", [""])[0],
                joined_date=params.get("joined_date", [""])[0],
                runs=params.get("runs", [""])[0],
                country=params.get("country", [""])[0],
                messaged=params.get("messaged", [""])[0],
                limit=_to_int(params.get("limit", ["0"])[0], 0),
            ))
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

        if parsed.path == "/api/message/mark":
            data = self._read_json_body()
            # Bulk form: {"ids": [...], "sent": true} -- used by the export
            # dialog to flag everything it just exported in one call.
            ids = data.get("ids")
            if isinstance(ids, list):
                n = _mark_sent_many([str(i) for i in ids],
                                    bool(data.get("sent", True)))
                self._send_json({"ok": True, "marked": n})
                return
            view = _mark_sent(data.get("id") or "", bool(data.get("sent")))
            if view is None:
                self._send_json({"ok": False, "error": "unknown contact id"}, 404)
            else:
                self._send_json({"ok": True, **view})
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
            for k in ("pages", "limit", "regions", "target",
                      "country", "gender", "age_min", "age_max",
                      "joined_op", "joined_date", "active_op", "active_date"):
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


def _to_float(value: str):
    """Parse a float, or None for blank/garbage. Negatives clamp to 0."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


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
