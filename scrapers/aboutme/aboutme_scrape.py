#!/usr/bin/env python3
"""Scrape public about.me user profiles into structured JSON.

about.me publishes every public profile in a sitemap, and each profile page
embeds the full profile object as JSON in a
`<script type="text/json" class="contextData">` tag. We:

  * /robots.txt -> Sitemap: .../SitemapIndex.xml
  * SitemapIndex.xml          -> ~225 child sitemaps (SitemapUser_<x>.xml)
  * SitemapUser_<x>.xml        -> 20k profile URLs each (https://about.me/<username>)
  * https://about.me/<username> -> contextData JSON -> name/role/location/bio/...

For every profile we emit one JSON object with the fields about.me actually
exposes. NOTE on the requested fields:

  * full name, role, location, summary, schools, interests, tags, links, image
    -> available.
  * age, gender, email -> about.me does NOT publish these. There is no such
    field on a profile (contact runs through a form, so emails are hidden).
    They are emitted as null. As a courtesy we also best-effort scan the bio
    and links for any email the user typed into their own text -> `emails`.

Output is JSONL (one user per line) so runs of millions of profiles stay
resumable and bounded in memory; re-running skips usernames already written.
A pretty JSON array (users.json) is also written when the set is small enough.

Usage:
    python scrapers/aboutme/aboutme_scrape.py [--limit 200] [--out users.jsonl]
                              [--json-out users.json] [--delay 0.3]
                              [--start-sitemap SitemapUser_abasuki.xml]
                              [--users alice,bob]

    --limit 0   scrape every public profile (~4.5M -- be sure). Default 0.

No external dependencies -- standard library plus scrapers/common/.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sys
import time
from pathlib import Path

# Make the shared `common` package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import extract_emails, fetch  # noqa: E402
from common.phones import extract_phones  # noqa: E402
from common.store import RecordStore  # noqa: E402

SITEMAP_INDEX = "https://aboutme-public.s3.amazonaws.com/sitemap/SitemapIndex.xml"
UA = "Mozilla/5.0 (compatible; aboutme-profile-scraper; polite)"
SCRIPT_DIR = Path(__file__).resolve().parent

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
CTX_RE = re.compile(
    r'<script type="text/json" class="contextData">(.*?)</script>', re.S
)
TAG_RE = re.compile(r"<[^>]+>")


def get_text(url: str):
    """GET text via the shared fetcher (None on 404/410)."""
    return fetch(url, parse="text", ua=UA)


# ---- profile-URL enumeration ----------------------------------------------
def child_sitemaps() -> list[str]:
    """Return the SitemapUser_*.xml child-sitemap URLs from the index."""
    raw = get_text(SITEMAP_INDEX)
    if not raw:
        raise RuntimeError(f"could not fetch sitemap index: {SITEMAP_INDEX}")
    locs = LOC_RE.findall(raw)
    return [u for u in locs if "SitemapUser_" in u]


def _write_cursor(path: str | None, sitemap_url: str) -> None:
    """Record the last child sitemap finished, so the next run resumes after it."""
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(sitemap_url.rsplit("/", 1)[-1])
    except OSError:
        pass


def iter_profile_urls(start_sitemap: str | None = None, cursor_out: str | None = None):
    """Yield every https://about.me/<username> URL, one child sitemap at a time.

    After each child sitemap is fully walked, its filename is written to
    `cursor_out` (if given) so a later run can `--start-sitemap` from there
    instead of re-walking the whole index.
    """
    children = child_sitemaps()
    if start_sitemap:
        children = [c for c in children if c.rsplit("/", 1)[-1] >= start_sitemap]
    print(f"  {len(children)} child sitemaps to walk", file=sys.stderr)
    for sm in children:
        # Record the cursor at the START of each sitemap so a --limit/stopped run
        # resumes from this same sitemap next time (already-saved usernames are
        # skipped), instead of restarting from the top of the index.
        _write_cursor(cursor_out, sm)
        raw = get_text(sm)
        if not raw:
            print(f"  ! failed sitemap {sm}", file=sys.stderr)
            continue
        for loc in LOC_RE.findall(raw):
            yield loc.strip()
        time.sleep(0.1)


def username_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


# ---- profile parsing ------------------------------------------------------
def clean_text(raw: str | None) -> str | None:
    """about.me bios arrive HTML- (often double-) escaped. Decode + strip tags."""
    if not raw:
        return None
    text = htmllib.unescape(raw)
    text = TAG_RE.sub(" ", text)
    text = htmllib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def pluck(items, *keys) -> list[str]:
    """Pull values from a list that may hold dicts ({key: val}) or bare strings."""
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in keys:
                v = it.get(k)
                if v:
                    out.append(v)
                    break
    return [s for s in (x.strip() for x in out) if s]


# Free-text locations have no country code, so "KR" is matched by Korea-related
# place names (incl. Hangul). "--exclude-location kr" expands to this set.
KOREA_TERMS = (
    "korea", "seoul", "busan", "incheon", "daegu", "gwangju", "daejeon",
    "suwon", "ulsan", "한국", "대한민국", "서울",
)


def build_exclude_terms(spec: str | None) -> list[str]:
    """Parse --exclude-location into lowercase match terms (kr -> Korea set)."""
    terms: set[str] = set()
    for t in (spec or "").split(","):
        t = t.strip().lower()
        if not t:
            continue
        if t in ("kr", "korea", "south korea", "republic of korea"):
            terms.update(KOREA_TERMS)
        else:
            terms.add(t)
    return sorted(terms)


def location_excluded(locations: list[str], terms: list[str]) -> bool:
    """True if any exclude term appears in the user's location text."""
    if not terms:
        return False
    hay = " ".join(locations or []).lower()
    return any(t in hay for t in terms)


def parse_profile(html: str, url: str) -> dict | None:
    """Extract a structured profile record from a profile page's HTML."""
    m = CTX_RE.search(html)
    if not m:
        return None
    try:
        ctx = json.loads(m.group(1))
        user = ctx["page"]["user"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None

    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full_name = " ".join(p for p in (first, last) if p) or None

    jobs = pluck(user.get("jobs"), "job", "title", "name")
    roles = pluck(user.get("roles"), "role", "name")
    locations = pluck(user.get("locations"), "location", "name")
    schools = pluck(user.get("schools"), "school", "name")
    interests = pluck(user.get("interests"), "interest", "name")
    tags = pluck(user.get("tags"), "tag", "name")
    summary = clean_text(user.get("bio"))
    links = user.get("links") or []

    contact = user.get("contact_me") or {}
    emails = extract_emails(" ".join(filter(None, (summary, json.dumps(links)))))

    return {
        "username": user.get("user_name") or username_of(url),
        "user_id": user.get("user_id"),
        "url": url,
        "full_name": full_name,
        "first_name": first or None,
        "last_name": last or None,
        # role: prefer an explicit job title, fall back to a declared role
        "role": (jobs or roles or [None])[0],
        "jobs": jobs,
        "roles": roles,
        "location": (locations or [None])[0],
        "locations": locations,
        "summary": summary,
        "schools": schools,
        "interests": interests,
        "tags": tags,
        # about.me does not publish these -- kept for the requested schema:
        "age": None,
        "gender": None,
        "email": (emails[0] if emails else None),
        "emails": emails,
        "contact_form_enabled": bool(contact.get("enabled")),
        "links": links,
    }


# ---- main -----------------------------------------------------------------
def scrape_user(username: str) -> dict | None:
    url = f"https://about.me/{username}"
    html = get_text(url)
    if not html:
        return None
    return parse_profile(html, url)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape public about.me profiles to JSON.")
    ap.add_argument("--limit", type=int, default=0,
                    help="max profiles to keep (0 = no limit, the default).")
    # Profiles go straight into the database, which is also where the resume
    # set comes from -- there is no output file to name.
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds between profile requests (default: 0.3)")
    ap.add_argument("--start-sitemap", default=None,
                    help="resume enumeration from this child sitemap filename")
    ap.add_argument("--cursor-out", default=None,
                    help="write the last finished child sitemap here (resume cursor)")
    ap.add_argument("--users", default=None,
                    help="comma-separated usernames to scrape instead of the sitemap")
    ap.add_argument("--exclude-location", default=None,
                    help="skip profiles whose location matches these terms "
                         "(comma-separated; 'kr' expands to Korea place names)")
    args = ap.parse_args()

    exclude_terms = build_exclude_terms(args.exclude_location)
    print("Only profiles whose content offers an email or phone number are kept.",
          file=sys.stderr)
    if exclude_terms:
        print(f"Filter: excluding locations matching {exclude_terms}", file=sys.stderr)

    store = RecordStore("aboutme")
    done = store.done_keys()
    if done:
        print(f"Resuming: {len(done)} profiles already stored.", file=sys.stderr)

    # build the source of usernames
    if args.users:
        urls = (f"https://about.me/{u.strip()}" for u in args.users.split(",") if u.strip())
    else:
        print(f"Enumerating profiles from {SITEMAP_INDEX} ...", file=sys.stderr)
        urls = iter_profile_urls(args.start_sitemap, args.cursor_out)

    kept = scanned = 0
    with store as out:
        for url in urls:
            if args.limit and kept >= args.limit:
                break
            username = username_of(url)
            if username in done:
                continue
            try:
                rec = scrape_user(username)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {username}: {e}", file=sys.stderr)
                time.sleep(1.5)
                continue
            if not rec:
                time.sleep(args.delay)
                continue
            scanned += 1

            # Keep a profile that offers any way to reach the person: an
            # email, or a phone / WhatsApp number in the summary or links.
            # The links are scanned as JSON, exactly as `parse_profile` scans
            # them for emails -- a wa.me / tel: URL lives in a link's `url`
            # value, and serialising keeps it intact whatever shape it is in.
            rec["phones"] = extract_phones(" ".join(filter(None, (
                rec.get("summary"), json.dumps(rec.get("links") or []),
            ))))
            if not rec["emails"] and not rec["phones"]:
                time.sleep(args.delay)
                continue
            if location_excluded(rec["locations"], exclude_terms):
                time.sleep(args.delay)
                continue

            out.add(rec)
            done.add(username)
            kept += 1
            note = f" | {rec['location']}" if rec["location"] else ""
            # A profile kept for a number alone has no address to show, so the
            # line names whichever contact it was actually kept for.
            reach = rec["email"] or (rec["phones"][0]["number"] if rec["phones"]
                                     else "")
            print(f"  [{kept}/{scanned} scanned] + {username}: {rec['full_name']} "
                  f"<{reach}>{note}", file=sys.stderr)
            time.sleep(args.delay)

    print(f"\nDone. Kept {kept} matching profiles ({scanned} scanned); "
          f"{store.count()} stored in total.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
