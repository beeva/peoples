#!/usr/bin/env python3
"""Reading scraper files into one common record shape, and merging by email.

This is the part of the pipeline that has nothing to do with storage: it turns
each scraper's own JSON/JSONL dialect into a single normalised record, then
groups the records that belong to the same person. ``dbsync.py`` writes the
result into MySQL; ``server.py`` imports the registry and the text helpers.

It lives in its own module because both of those need it and neither should
import the other.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCRAPERS_DIR = BASE_DIR / "scrapers"

# The scrapers decide which of a person's addresses is the one to write to; the
# API reads that same rule rather than keeping a second opinion. Applying it on
# load also means records scraped before the rule existed are ordered correctly
# without re-scraping them.
sys.path.insert(0, str(SCRAPERS_DIR))
from common import personal_first  # noqa: E402

DISCOURSE_FILE = SCRAPERS_DIR / "discourse" / "threejs" / "threejs_emails.jsonl"
DEVTO_FILE = SCRAPERS_DIR / "devto" / "jobs.json"
ABOUTME_FILE = SCRAPERS_DIR / "aboutme" / "users.jsonl"
GITHUB_FILE = SCRAPERS_DIR / "github" / "users.jsonl"

# Strip HTML tags to build a clean text preview / searchable blob.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MD_RE = re.compile(r"[#>*_`~\[\]()!]+")  # light markdown noise


def to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_html(html: str) -> str:
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


def preview(text: str, limit: int = 320) -> str:
    text = strip_html(text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def md_preview(markdown: str, limit: int = 320) -> str:
    text = _MD_RE.sub(" ", markdown or "")
    return preview(text, limit)


def md_clean(markdown: str) -> str:
    """Full markdown -> plain text, untruncated (for the detail page)."""
    return strip_html(_MD_RE.sub(" ", markdown or ""))


def parse_ts(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %Y")


def read_jsonl(path: Path):
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
                print(f"[warn] {path.name}: skipping malformed line {line_no}",
                      file=sys.stderr)


def read_json_array(path: Path):
    if not path.exists():
        print(f"[warn] data file not found: {path}", file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[warn] {path.name}: malformed JSON", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def norm_tags(tags) -> list[str]:
    """dev.to tag_list is sometimes a list, sometimes a comma string."""
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in (tags or []) if str(t).strip()]


def norm_links(links) -> list[str]:
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
def record(source, local_id, *, emails, name="", username="", title="",
           url="", created_at="", preview_text="", tags=None, location="",
           organization="", apply_links=None, messaging=None, links=None,
           search_extra="", full="", site_url="", activity_at="", run=0,
           country="", country_code="", gender="", raw=None):
    emails = [e.lower() for e in (emails or []) if e]
    seen, uniq = set(), []
    for e in emails:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    rec = {
        "id": f"{source}:{local_id}",
        "source": source,
        "local_id": str(local_id),
        # The scraper's own record, kept whole. Everything below is a lossy
        # projection of it, and the database is the only place it now lives.
        "raw": raw if isinstance(raw, dict) else {},
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
        "scraped_country": (country or "").strip(),
        "scraped_country_code": (country_code or "").strip().upper()[:2],
        "scraped_gender": (gender or "").strip().lower(),
        "preview": preview_text or "",
        "tags": tags or [],
        "location": (location or "").strip(),
        "organization": (organization or "").strip(),
        "apply_links": apply_links or [],
        "messaging": messaging or [],
        "links": links or [],
    }
    # Full, untruncated cleaned text of this single occurrence -- shown on the
    # detail page (the list only needs `preview`). Falls back to the preview.
    rec["full"] = (full or rec["preview"] or "").strip()
    # Sort on the date the UI shows: for a profile that is "last active", for a
    # post it is when it was written. Keeps the Date column and its sort honest.
    rec["ts"] = parse_ts(activity_at or created_at or "")
    # The join date on its own, for the account-age filter (0.0 = unknown).
    rec["created_ts"] = parse_ts(created_at or "")
    rec["activity_ts"] = parse_ts(activity_at or "")
    rec["blob"] = " ".join([
        " ".join(uniq), rec["name"], rec["username"], rec["title"],
        rec["location"], rec["organization"], " ".join(rec["tags"]),
        rec["full"], search_extra,
    ]).lower()
    return rec


# ---- per-source builders --------------------------------------------------
# Each source has a `build(raw, i)` that turns ONE line of its file into a
# record, and a `load()` that maps it over the whole file. Keeping the
# single-line form separate is what lets dbsync ingest just the lines a running
# scraper appended, instead of re-reading a 7MB archive every couple of
# seconds. `i` is only the 1-based line number, used as an id of last resort.
def build_discourse(d: dict, i: int) -> dict:
    return record(
        "discourse", d.get("post_id", i),
        emails=d.get("emails", []),
        name=d.get("name") or "",
        username=d.get("username") or "",
        title=d.get("topic_title", ""),
        url=d.get("post_url") or d.get("topic_url") or "",
        created_at=d.get("created_at", ""),
        preview_text=preview(d.get("cooked", "")),
        full=strip_html(d.get("cooked", "")),
        search_extra=d.get("topic_title", ""),
        raw=d,
    )


def build_devto(d: dict, i: int) -> dict:
    contact = d.get("contact") or {}
    emails = list(contact.get("emails", [])) + list(contact.get("mailto", []))
    return record(
        "devto", d.get("id", i),
        emails=emails,
        name=d.get("author") or "",
        title=d.get("title", ""),
        url=d.get("url") or "",
        created_at=d.get("published_at", ""),
        preview_text=md_preview(d.get("description", "")),
        full=md_clean(d.get("description", "")),
        tags=norm_tags(d.get("tags")),
        organization=d.get("organization") or "",
        apply_links=contact.get("apply_links", []),
        messaging=contact.get("messaging", []),
        raw=d,
    )


def build_aboutme(d: dict, i: int) -> dict:
    username = d.get("username") or str(d.get("user_id") or i)
    return record(
        "aboutme", username,
        emails=d.get("emails", []),
        name=d.get("full_name") or username,
        username=username,
        title=d.get("role") or "",
        url=d.get("url") or "",
        created_at="",  # about.me profiles have no timestamp
        preview_text=preview(d.get("summary") or ""),
        full=strip_html(d.get("summary") or ""),
        location=d.get("location") or "",
        links=norm_links(d.get("links")),
        search_extra=" ".join(d.get("schools", []) + d.get("interests", [])),
        raw=d,
    )


def build_github(d: dict, i: int) -> dict:
    login = d.get("login") or str(d.get("user_id") or i)
    bio = d.get("bio") or ""
    # Which sources the email came from ("profile", "site", ...) -- worth
    # surfacing as tags: a profile email is a warmer lead than a commit one.
    via = list((d.get("email_sources") or {}).keys())
    twitter = d.get("twitter")
    # The GitHub profile is `url`; the portfolio site is its own field. Any
    # Twitter handle rounds out the "links" list shown on the detail card.
    links = [f"https://twitter.com/{twitter}"] if twitter else []
    return record(
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
        run=to_int(d.get("run"), 1),
        country=d.get("country") or "",            # from regions.py classify()
        country_code=d.get("country_code") or "",
        gender=d.get("gender") or "",              # set only by a --gender run
        preview_text=preview(bio),
        full=bio,
        tags=[t for t in [d.get("country")] if t] + [f"via {v}" for v in via],
        location=d.get("location") or "",
        organization=d.get("company") or "",
        links=links,
        search_extra=" ".join(filter(None, (
            d.get("country"), d.get("region"), d.get("site_url"), twitter,
        ))),
        raw=d,
    )


def _loader(build, path: Path, reader):
    def load():
        return [build(d, i) for i, d in enumerate(reader(path), 1)]
    return load


load_discourse = _loader(build_discourse, DISCOURSE_FILE, read_jsonl)
load_aboutme = _loader(build_aboutme, ABOUTME_FILE, read_jsonl)
load_github = _loader(build_github, GITHUB_FILE, read_jsonl)
load_devto = _loader(build_devto, DEVTO_FILE, read_json_array)


# ---- source registry ------------------------------------------------------
# `jsonl` marks a source whose file is only ever appended to, which is what
# makes an incremental (new-lines-only) ingest safe.
SOURCES = [
    {"key": "discourse", "label": "three.js forum", "noun": "Posts",
     "loader": load_discourse, "build": build_discourse,
     "file": DISCOURSE_FILE, "jsonl": True},
    {"key": "aboutme", "label": "about.me", "noun": "Profiles",
     "loader": load_aboutme, "build": build_aboutme,
     "file": ABOUTME_FILE, "jsonl": True},
    {"key": "github", "label": "GitHub", "noun": "Profiles",
     "loader": load_github, "build": build_github,
     "file": GITHUB_FILE, "jsonl": True},
]
# dev.to is scraped and stored, but is not one of the tabs -- it was dropped
# from the listed sources before the database existed. Keeping it here means its
# data is preserved and re-scrapable; keeping it out of SOURCES means the UI is
# unchanged. `dbquery` spells "all" out as the listed keys rather than "no
# source filter" precisely so a stored-but-unlisted source cannot leak in.
DEVTO_SOURCE = {"key": "devto", "label": "dev.to jobs", "noun": "Posts",
                "loader": load_devto, "build": build_devto,
                "file": DEVTO_FILE, "jsonl": False}

STORED_SOURCES = SOURCES + [DEVTO_SOURCE]
LISTED_KEYS = [s["key"] for s in SOURCES]

SOURCE_BY_KEY = {s["key"]: s for s in STORED_SOURCES}
NOUNS = {s["key"]: s["noun"] for s in STORED_SOURCES}
NOUNS["all"] = "Records"


# ---- merge same-email rows ------------------------------------------------
def merge_group(group: list[dict]) -> dict:
    """Collapse records that belong to one contact into a single row.

    The newest record is the representative (its title/url/preview head the
    card); every other occurrence is kept under ``posts`` so nothing is lost.
    List fields (emails, tags, links) are unioned, order-preserving.

    Country/gender here are only what the *scraper* worked out. Claude's
    enrichment is stored per email in its own table and layered on top by the
    database, so re-ingesting a file never throws an inference away.
    """
    group = sorted(group, key=lambda r: r["ts"], reverse=True)
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
    rep["scraped_country"] = first("scraped_country")
    rep["scraped_country_code"] = first("scraped_country_code")
    rep["scraped_gender"] = first("scraped_gender")
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
    rep["ts"] = max(r["ts"] for r in group)
    rep["activity_ts"] = max(r["activity_ts"] for r in group)
    rep["blob"] = " ".join(r["blob"] for r in group)
    rep["member_ids"] = [r["id"] for r in group]
    return rep


def merge_by_email(recs: list[dict]) -> list[dict]:
    """Group a source's records so each contact email appears on one row.

    Records are linked when they share any email (union-find), so a person who
    posted the same address across many posts collapses to a single row even if
    some posts list extra addresses. Records with no email stay on their own.
    """
    parent = list(range(len(recs)))

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
    for idx, rec in enumerate(recs):
        for e in rec["emails"]:
            if e in seen_email:
                link(seen_email[e], idx)
            else:
                seen_email[e] = idx

    groups: dict[int, list[dict]] = {}
    for idx in range(len(recs)):
        groups.setdefault(find(idx), []).append(recs[idx])
    return [merge_group(g) for g in groups.values()]
