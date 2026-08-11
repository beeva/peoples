#!/usr/bin/env python3
"""Revisit every stored contact's own pages looking for phone / WhatsApp numbers.

A profile almost never carries a phone number -- of 10,770 stored GitHub
profiles, the bio yielded two. The number, when someone publishes one at all,
is on their own site: a contact page, a footer, a `wa.me` button. Those pages
were fetched once for an email address and not kept, so finding phones means
fetching them again.

That is thousands of requests to other people's servers, which is why this is a
separate command rather than part of a scrape:

    npm run scrape:phones                    # every stored contact with a site
    npm run scrape:phones -- --limit 200
    npm run scrape:phones -- --source github
    python scrapers/phone_pass.py --no-readme

Every source is covered, because each one stores somewhere to go back to:

    github     the profile's `blog` field, plus the <login>/<login> README
    aboutme    the profile's own links, and any URL in its summary
    discourse  sites the person linked from their posts
    devto      the post's apply / messaging links

It is **resumable** (records already visited are recorded and skipped), **safe
to stop** at any point (each hit is stored as it is found), and throttled the
same way the scrapers are. Numbers are written onto the stored record, so the
extraction that follows sees them exactly as a fresh scrape would.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # scrapers/
sys.path.insert(0, str(SCRIPT_DIR.parent))            # project root
sys.path.insert(0, str(SCRIPT_DIR))                   # scrapers/
sys.path.insert(0, str(SCRIPT_DIR / "github"))        # github_scrape

from common import fetch, load_env  # noqa: E402
from common.phones import extract_phones, merge_phones, phone_first  # noqa: E402
from common.store import _load_project_env  # noqa: E402
from github_scrape import CONTACT_PATHS, UA, site_url_of  # noqa: E402

import db  # noqa: E402
import dbphones  # noqa: E402
import dbsync  # noqa: E402

# Where a personal site hides its contact details. The homepage first, because
# a footer number is the common case and costs one request.
PHONE_PATHS = ("",) + tuple(CONTACT_PATHS)

# These fetches are opportunistic: we are re-visiting sites we already have the
# email from, hoping for a bonus. So they give up fast. A single try with a
# short timeout costs a dead host ~8s instead of ~40s, and across 6,000 mostly
# dead personal sites that is the difference between one hour and two days.
FETCH_TIMEOUT = 8
FETCH_TRIES = 1

URL_RE = re.compile(r"""https?://[^\s"'<>)\]}]+""", re.I)

# Hosts with nothing to find. Two kinds, and both are worth skipping for the
# same reason -- the request costs as much as a useful one:
#   * profile silos, where a number would be behind a login if it existed;
#   * the forum's and GitHub's own asset hosts, which turn up in post HTML.
# `linktr.ee` and its kind are deliberately NOT here: a link-in-bio page is
# mostly a list of contact buttons, and a WhatsApp button is exactly the thing
# this pass is looking for.
SKIP_HOST_RE = re.compile(
    r"(^|\.)("
    r"github\.com|githubusercontent\.com|githubassets\.com|gist\.github\.com"
    r"|linkedin\.com|twitter\.com|x\.com|facebook\.com|fb\.com"
    r"|instagram\.com|youtube\.com|youtu\.be|tiktok\.com|snapchat\.com"
    r"|medium\.com|stackoverflow\.com|stackexchange\.com|npmjs\.com"
    r"|reddit\.com|pinterest\.[a-z.]+|tumblr\.com|flickr\.com"
    r"|plus\.google\.com|docs\.google\.com|drive\.google\.com"
    r"|t\.me|telegram\.me|discord\.gg|discord\.com|slack\.com"
    r"|mastodon\.[a-z.]+|gamedev\.place|bsky\.app|threads\.net"
    r"|about\.me|dev\.to|hashnode\.(?:com|dev)|substack\.com"
    r"|wikipedia\.org|w3\.org|schema\.org|creativecommons\.org"
    r"|amazonaws\.com|cloudfront\.net|gravatar\.com|imgur\.com"
    r"|discourse\.threejs\.org|threejs\.org|codepen\.io|jsfiddle\.net"
    r"|npmjs\.org|nodejs\.org|mozilla\.org|apple\.com|microsoft\.com"
    r")$", re.I)

# Not a page: an asset, a feed, a download. Fetching one costs a request and
# yields bytes no number could be read out of.
SKIP_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|avif|ico|bmp|pdf|zip|t?gz|rar|7z|dmg|exe"
    r"|mp[34]|m4[av]|mov|webm|wav|ogg|css|js|mjs|map|json|xml|rss|atom"
    r"|txt|csv|woff2?|ttf|eot)(?:[?#]|$)", re.I)

# How many different sites one contact is worth. Almost everybody has one; the
# cap is there so a post that pasted twenty links cannot cost twenty crawls.
MAX_URLS_PER_RECORD = 3


def _clean_url(raw: str) -> str:
    """A fetchable page URL from something found in text, or "" to skip it.

    ``site_url_of`` does the hard part (bare hosts, bare emails, @handles);
    this adds what only matters when the URL came out of prose or markup --
    trailing punctuation the sentence owns rather than the link.
    """
    url = (raw or "").strip().rstrip('.,;:!?)"\'>]}')
    if not url:
        return ""
    url = site_url_of(url)
    if not url or SKIP_EXT_RE.search(url):
        return ""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host or SKIP_HOST_RE.search(host):
        return ""
    return url


def _urls_of(source: str, row: dict, raw: dict) -> list[str]:
    """Where this record says its person can be found, best first.

    Named per source rather than swept out of the whole JSON blob: a record
    also carries avatar URLs, API hrefs and the forum's own links, and each of
    those is a request that was never going to yield anything.
    """
    urls: list[str] = []
    if source == "github":
        # The profile's `blog` -- the one link the person chose to publish.
        urls.append(row.get("site_url") or raw.get("site_url") or "")
    elif source == "aboutme":
        for item in raw.get("links") or []:
            if isinstance(item, dict):
                urls.append(item.get("url") or item.get("href") or "")
            else:
                urls.append(str(item))
        urls += URL_RE.findall(raw.get("summary") or "")
    elif source == "discourse":
        # Whatever they linked from their posts -- for a forum, the portfolio
        # in a "here is my work" reply is the personal site.
        urls += URL_RE.findall(raw.get("cooked") or "")
    elif source == "devto":
        contact = raw.get("contact") or {}
        urls += list(contact.get("apply_links") or [])
        urls += list(contact.get("messaging") or [])
        urls += URL_RE.findall(raw.get("description") or "")

    out: list[str] = []
    seen: set[str] = set()
    for candidate in urls:
        url = _clean_url(candidate)
        # De-duplicate by host: /page-one and /page-two of the same site are
        # one crawl, and the crawl below already walks a site's contact pages.
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if not url or host in seen:
            continue
        seen.add(host)
        out.append(url)
        if len(out) >= MAX_URLS_PER_RECORD:
            break
    return out


def _page_text(url: str) -> tuple[str, bool]:
    """(html, reachable). "" with reachable=False means the host is not there."""
    try:
        html = fetch(url, parse="text", ua=UA, tries=FETCH_TRIES,
                     timeout=FETCH_TIMEOUT,
                     none_on=(400, 401, 403, 404, 405, 410, 451))
    except Exception:  # noqa: BLE001 -- a broken personal site is routine
        return "", False
    # A None/empty body from a *served* status is still a reachable host; only
    # an exception means we could not talk to it at all.
    return (html or ""), True


def phones_from_site(url: str, delay: float) -> list[dict]:
    """Scan a portfolio site for numbers, homepage first then contact pages.

    Unlike the email crawl this does *not* stop at the first page that yields
    something: a site often shows a WhatsApp button on /contact and a plain
    number in the footer, and both are worth having.

    If the homepage cannot be reached at all the host is dead or gone, and the
    four contact paths would each pay the same timeout to learn the same thing
    -- so we stop.
    """
    found: list[dict] = []
    base = url.rstrip("/")
    for i, path in enumerate(PHONE_PATHS):
        if i:
            time.sleep(delay)
        # The raw HTML, not stripped text -- wa.me and tel: live in href
        # attributes, which is the strongest evidence available.
        html, reachable = _page_text(base + path if path else url)
        if i == 0 and not reachable:
            return []
        if html:
            found = merge_phones(found, extract_phones(html))
        # A WhatsApp link is as good as it gets; no reason to keep crawling.
        if any(p["whatsapp"] for p in found):
            break
    return phone_first(found)


def phones_from_readme(login: str) -> list[dict]:
    """Numbers in the <login>/<login> profile README."""
    url = f"https://raw.githubusercontent.com/{login}/{login}/HEAD/README.md"
    try:
        text = fetch(url, parse="text", ua=UA, tries=FETCH_TRIES,
                     timeout=FETCH_TIMEOUT, none_on=(400, 403, 404, 410))
    except Exception:  # noqa: BLE001
        return []
    return extract_phones(text or "")


def scan_record(row: dict, delay: float, use_readme: bool) -> list[dict]:
    """Every number findable for one stored record. Network only -- no database.

    Kept free of database access on purpose: it runs on worker threads, and
    keeping the writes on the main thread means no connection-per-thread churn
    and no locking around the resume state.
    """
    found: list[dict] = []
    for i, url in enumerate(row["urls"]):
        if i:
            time.sleep(delay)
        found = merge_phones(found, phones_from_site(url, delay))
        if any(p["whatsapp"] for p in found):
            break
    if (row["source"] == "github" and use_readme
            and not any(p["whatsapp"] for p in found)):
        found = merge_phones(found, phones_from_readme(row["local_id"]))
    return phone_first(found)


def _store(record_id: str, phones: list[dict]) -> None:
    """Save a record's numbers to the column *and* into its stored raw JSON.

    Read-modify-write in Python rather than SQL JSON functions: MariaDB 10.4
    treats JSON as LONGTEXT and has no CAST(... AS JSON), so building the
    document here is both portable and easier to follow. Only ~5% of visited
    records have a number, so the extra read is negligible.
    """
    blob = json.dumps(phones, ensure_ascii=False)
    try:
        raw = json.loads(db.scalar("SELECT raw FROM records WHERE id = %s",
                                   (record_id,), default="{}") or "{}")
    except ValueError:
        raw = {}
    raw["phones"] = phones
    db.execute("UPDATE records SET phones = %s, raw = %s WHERE id = %s",
               (blob, json.dumps(raw, ensure_ascii=False), record_id))


def _candidates(source: str, only_missing: bool) -> list[dict]:
    """Stored records worth visiting, most recently active first.

    Only records that name somewhere to go are worth a request -- without one
    there is nowhere new to look, and their own text has already been scanned
    offline by ``npm run db:phones``.
    """
    where = "source = %s"
    params: list = [source]
    if only_missing:
        # Skip anyone a previous pass already found a number for.
        where += " AND (phones IS NULL OR phones = '[]')"
    rows = db.query(
        f"SELECT id, source, local_id, site_url, phones, raw FROM records "
        f"WHERE {where} ORDER BY ts DESC", params)

    out = []
    for row in rows:
        try:
            raw = json.loads(row["raw"] or "{}")
        except ValueError:
            raw = {}
        urls = _urls_of(source, row, raw if isinstance(raw, dict) else {})
        # GitHub is the exception: the profile README is worth a look even for
        # someone who published no site at all, and it costs one request.
        if urls or source == "github":
            out.append({"id": row["id"], "source": source,
                        "local_id": row["local_id"], "urls": urls,
                        "phones": row["phones"]})
    return out


VISITED_KEY = "phone_pass_visited"
# Where the GitHub-only version of this pass kept its progress. Read once, so
# a run that already visited 3,500 profiles does not pay for them again.
LEGACY_GITHUB_KEY = "github_phone_pass_visited"
# Cap on the resume set per source. This is an aid, not a record: past it the
# oldest visits are forgotten and those records are looked at again one day.
VISITED_CAP = 200000
# How often the resume set is written. This run is long, it is *expected* to be
# stopped part way, and it can be stopped by something that does not let it
# finish tidily -- a shell timeout, a reboot. So the interval is set by what a
# kill may cost, not by what a write costs: one row of JSON every 25 records is
# nothing beside re-fetching 25 sites.
SAVE_EVERY = 25


def _load_visited() -> dict:
    """{source: {record id: None}} -- dicts used as ordered sets."""
    state = db.state_get_all()
    out: dict = {}
    for source, ids in (state.get(VISITED_KEY) or {}).items():
        out[source] = dict.fromkeys(ids or [])
    legacy = (state.get(LEGACY_GITHUB_KEY) or {}).get("logins") or []
    if legacy:
        # The old key stored bare logins; ids are "github:<login>".
        github = out.setdefault("github", {})
        for login in legacy:
            github.setdefault(f"github:{login}", None)
    return out


def _save_visited(visited: dict) -> None:
    # Trimmed from the *front*, so what is dropped is the oldest visit rather
    # than whatever sorts first -- an alphabetical cap would forget the same
    # records on every run and re-visit them forever.
    db.state_put(VISITED_KEY,
                 {source: list(ids)[-VISITED_CAP:]
                  for source, ids in visited.items()})


def run_source(source: str, args, visited: dict) -> dict:
    """Visit one source's records. Returns what it found."""
    seen = visited.setdefault(source, {})
    rows = _candidates(source, only_missing=not args.all)
    todo = [r for r in rows if r["id"] not in seen]
    if args.limit > 0:
        todo = todo[:args.limit]

    print(f"\n== {source}: {len(rows)} records have somewhere to look; "
          f"{len(seen)} already visited; scanning {len(todo)} this run.",
          file=sys.stderr)
    if not todo:
        print("   Nothing to do.", file=sys.stderr)
        return {"scanned": 0, "found": 0, "whatsapp": 0}

    found_total = wa_total = 0
    started = time.time()
    # Every record is a different host, so fetching several at once is both
    # much faster and no less polite than one at a time -- no single site sees
    # more than one worker. The database writes stay on this thread.
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futures = {pool.submit(scan_record, row, args.delay,
                               not args.no_readme): row for row in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                phones = fut.result()
            except Exception as e:  # noqa: BLE001 -- one bad site is not fatal
                print(f"   ! {row['id']}: {e}", file=sys.stderr)
                phones = []

            seen[row["id"]] = None
            if phones:
                try:
                    existing = json.loads(row["phones"] or "[]")
                except ValueError:
                    existing = []
                merged = phone_first(merge_phones(existing, phones))
                # Written into `raw` as well as the column. `raw` is what a
                # scrape produced, and a fresh scrape *would* now return these
                # (the scrapers harvest the same pages) -- so putting them
                # there keeps the two consistent, and stops the re-extraction
                # in dbphones, which rebuilds the column from `raw`, from
                # discarding everything this pass just found.
                _store(row["id"], merged)
                found_total += 1
                wa = any(p["whatsapp"] for p in merged)
                wa_total += 1 if wa else 0
                note = " (whatsapp)" if wa else ""
                print(f"   [{i}/{len(todo)}] + {row['local_id']}: "
                      f"{merged[0]['number']}{note}", file=sys.stderr)
            elif i % 100 == 0:
                rate = i / max(1e-9, time.time() - started)
                left = (len(todo) - i) / max(1e-9, rate) / 60
                print(f"   [{i}/{len(todo)}] … {found_total} found, "
                      f"{rate:.1f} rec/s, ~{left:.0f} min left", file=sys.stderr)

            if i % SAVE_EVERY == 0:
                _save_visited(visited)
    finally:
        # Do not wait for in-flight fetches on the way out; they are only ever
        # worth a number we can find again next run.
        pool.shutdown(wait=False, cancel_futures=True)
        _save_visited(visited)
        # Fold whatever was found into the merged contacts.
        dbphones.backfill(source, verbose=False)

    return {"scanned": len(todo), "found": found_total, "whatsapp": wa_total}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Revisit stored contacts' own sites looking for phone / "
                    "WhatsApp numbers.")
    ap.add_argument("--source", default="",
                    help="one source (github, aboutme, discourse, devto); "
                         "default: all of them")
    ap.add_argument("--limit", type=int, default=0,
                    help="max records to visit per source this run (0 = all)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds between a site's own pages (default: 0.3). "
                         "Different records are different hosts, so this only "
                         "paces requests to one site")
    ap.add_argument("--workers", type=int, default=12,
                    help="records fetched in parallel (default: 12). Each "
                         "worker is on a different host, so no site sees more "
                         "than one")
    ap.add_argument("--no-readme", action="store_true",
                    help="skip the GitHub profile README")
    ap.add_argument("--all", action="store_true",
                    help="revisit records that already have a number too")
    ap.add_argument("--restart", action="store_true",
                    help="forget which records have been visited and start over")
    args = ap.parse_args()

    # A run this long is usually ended by something other than itself -- a
    # shell timeout, a service manager, `npm run kill`. Turning the polite
    # signal into the interrupt the code already handles means those all take
    # the tidy exit: resume set written, findings folded in. Ctrl-C is
    # KeyboardInterrupt already; SIGTERM would otherwise kill it where it stood.
    def _on_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)

    _load_project_env()
    load_env(SCRIPT_DIR.parent / ".env")
    db.bootstrap(verbose=False)
    # The pass folds what it finds into the merged contacts, so they have to be
    # there -- a schema bump drops them and leaves them for whoever boots next.
    dbsync.ensure_contacts_rebuilt()

    known = {s["key"] for s in dbsync.rec_mod.STORED_SOURCES}
    if args.source and args.source not in known:
        print(f"unknown source '{args.source}' (expected one of "
              f"{', '.join(sorted(known))})", file=sys.stderr)
        return 2
    sources = ([args.source] if args.source
               else [s["key"] for s in dbsync.rec_mod.STORED_SOURCES])

    visited: dict = {} if args.restart else _load_visited()
    totals = {}
    try:
        for source in sources:
            totals[source] = run_source(source, args, visited)
    except KeyboardInterrupt:
        print("\nStopped -- everything found so far is stored.", file=sys.stderr)

    # Two finishing passes, because per-source `backfill` above only refreshes
    # each contact's own numbers. Neither can be folded into it:
    #   * `revalidate` judges a number against the whole archive -- one held by
    #     a crowd of unrelated people came off a page, not a person, and that
    #     is only visible once every source has been walked;
    #   * `rebuild_contacts` re-runs the merge, which is what collapses two
    #     contacts that this pass has just discovered share a number.
    print("\nFolding the results in…", file=sys.stderr)
    dbphones.revalidate()
    dbsync.rebuild_contacts(verbose=False)

    print("\nDone.", file=sys.stderr)
    status = dbphones.status()
    for source in sources:
        got = totals.get(source) or {"scanned": 0, "found": 0, "whatsapp": 0}
        st = status.get(source, {})
        print(f"  {source:<10} visited {got['scanned']:>6}, "
              f"{got['found']:>4} with a number ({got['whatsapp']} on WhatsApp)"
              f"  ->  {st.get('with_phone', 0)} contacts reachable, "
              f"{st.get('with_whatsapp', 0)} on WhatsApp", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
