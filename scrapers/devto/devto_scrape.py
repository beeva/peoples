#!/usr/bin/env python3
"""dev.to job-description email/contact scraper.

Job posts on dev.to are regular articles tagged #hiring / #forhire / #jobs.
The Forem (dev.to) public API exposes the full article body as `body_markdown`,
so we can pull each post and scan it for contact info. Any job description that
contains contact info (email, mailto, telegram, ATS/careers link, etc.) is
emitted as JSON.

Usage:
    python scrapers/devto/devto_scrape.py [--tags hiring,forhire,jobs]
                     [--pages 3] [--per-page 50] [--out jobs.json]

No external dependencies -- uses only the Python standard library plus the
shared helpers in scrapers/common/.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

# Make the shared `common` package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import extract_emails, extract_mailto, fetch  # noqa: E402

API = "https://dev.to/api"
UA = "devto-job-scraper"
SCRIPT_DIR = Path(__file__).resolve().parent


# ---- contact-info extractors ----------------------------------------------
# Emails / mailto come from scrapers/common; apply-links and messaging are
# dev.to-specific (job posts often route applications through ATS / Telegram).
URL_RE = re.compile(r"https?://[^\s\"')>\]]+", re.I)
APPLY_HOST = re.compile(
    r"(lever\.co|greenhouse\.io|workable\.com|jobs\.|boards\.|apply|careers"
    r"|recruiting|smartrecruiters|bamboohr|ashbyhq|jobvite|breezy\.hr"
    r"|typeform\.com/to)",
    re.I,
)
MSG_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me|discord\.gg|discord\.com/invite)/[^\s\"')>\]]+",
    re.I,
)


def _strip_trailing(url: str) -> str:
    return re.sub(r"[.,)]+$", "", url)


def extract_apply_links(text: str) -> list[str]:
    out = set()
    for url in URL_RE.findall(text):
        url = _strip_trailing(url)
        if APPLY_HOST.search(url):
            out.add(url)
    return sorted(out)


def extract_messaging(text: str) -> list[str]:
    return sorted({_strip_trailing(m) for m in MSG_RE.findall(text)})


def has_contact(c: dict) -> bool:
    return any((c["emails"], c["mailto"], c["apply_links"], c["messaging"]))


def get_json(url: str):
    """GET + parse JSON via the shared fetcher (retry/back-off built in)."""
    return fetch(url, parse="json", accept="application/json", ua=UA,
                 tries=3, none_on=())


# ---- main -----------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape dev.to job posts for contact info.")
    ap.add_argument("--tags", default="hiring,forhire,jobs",
                    help="comma-separated dev.to tags (default: hiring,forhire,jobs)")
    ap.add_argument("--pages", type=int, default=3, help="pages per tag (default: 3)")
    ap.add_argument("--per-page", type=int, default=50, help="articles per page (default: 50)")
    ap.add_argument("--out", default=str(SCRIPT_DIR / "jobs.json"),
                    help="output JSON file (default: scrapers/devto/jobs.json)")
    args = ap.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    print(f"Scraping dev.to tags=[{', '.join(tags)}] pages={args.pages} "
          f"per_page={args.per_page}", file=sys.stderr)

    # 1. Collect candidate article stubs across tags + pages, deduped by id.
    stubs: dict[int, dict] = {}
    for tag in tags:
        for page in range(1, args.pages + 1):
            url = f"{API}/articles?tag={urllib.parse.quote(tag)}&per_page={args.per_page}&page={page}"
            try:
                lst = get_json(url)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {tag} p{page}: {e}", file=sys.stderr)
                break
            if not isinstance(lst, list) or not lst:
                break
            for a in lst:
                stubs.setdefault(a["id"], a)
            print(f"  tag={tag} page={page}: +{len(lst)} (total unique {len(stubs)})",
                  file=sys.stderr)
            time.sleep(0.3)  # be polite

    # 2. Fetch each full article and scan its body for contact info.
    results = []
    total = len(stubs)
    for i, stub in enumerate(stubs.values(), 1):
        try:
            art = get_json(f"{API}/articles/{stub['id']}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! article {stub['id']}: {e}", file=sys.stderr)
            continue
        body = f"{art.get('title', '')}\n{art.get('body_markdown') or ''}"
        contact = {
            "emails": extract_emails(body, include_mailto=False),
            "mailto": extract_mailto(body),
            "apply_links": extract_apply_links(body),
            "messaging": extract_messaging(body),
        }
        title = art.get("title", "")
        if has_contact(contact):
            results.append({
                "id": art.get("id"),
                "title": title,
                "url": art.get("url"),
                "published_at": art.get("published_at"),
                "author": (art.get("user") or {}).get("name"),
                "organization": (art.get("organization") or {}).get("name"),
                "tags": art.get("tag_list"),
                "reading_time_minutes": art.get("reading_time_minutes"),
                "contact": contact,
                "description": art.get("body_markdown"),
            })
            print(f"  [{i}/{total}] + contact found: {title[:60]}", file=sys.stderr)
        else:
            print(f"  [{i}/{total}] - no contact: {title[:60]}", file=sys.stderr)
        time.sleep(0.25)  # polite throttle

    # 3. Output.
    results.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nDone. {len(results)}/{total} job posts had contact info -> {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
