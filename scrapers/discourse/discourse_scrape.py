#!/usr/bin/env python3
"""Scrape ALL posts from a Discourse forum (default: discourse.threejs.org).

Discourse exposes a clean JSON API:
  * /sitemap.xml -> child sitemaps -> every topic URL (/t/{slug}/{id})
  * /t/{id}.json -> topic metadata + post_stream.posts (first chunk)
                    + post_stream.stream (ALL post ids in the topic)
  * /t/{id}/posts.json?post_ids[]=... -> fetch remaining posts in batches

This walks every topic and writes one JSON object per topic (with all its
posts) as a line of JSONL. It is resumable: re-running skips topics whose id
already appears in the output file.

Usage:
    python scrapers/discourse/discourse_scrape.py
                               [--base https://discourse.threejs.org]
                               [--out threejs_posts.jsonl]
                               [--limit 0] [--delay 0.5] [--batch 50]

    --limit 0   scrape every topic (default). Set e.g. 50 for a quick test.

No external dependencies -- standard library plus scrapers/common/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

# Make the shared `common` package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import extract_emails, fetch, load_done_keys  # noqa: E402

DEFAULT_BASE = "https://discourse.threejs.org"
UA = "discourse-post-scraper (polite; contact via forum)"

# Per-site output lives in a sibling folder next to this engine, e.g.
#   scrapers/discourse/threejs/threejs_{posts,emails}.jsonl
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SITE = "threejs"

# Fields we keep per post (the raw post object has ~60 fields, most noise).
POST_FIELDS = (
    "id", "post_number", "user_id", "username", "name", "created_at",
    "updated_at", "reply_to_post_number", "reply_count", "quote_count",
    "incoming_link_count", "score", "accepted_answer", "post_type", "cooked",
)


def get_json(url: str):
    return fetch(url, parse="json", accept="application/json", ua=UA)


def get_text(url: str):
    return fetch(url, parse="text", ua=UA)


# ---- topic enumeration ----------------------------------------------------
TOPIC_URL_RE = re.compile(r"/t/(?:[^/]+/)?(\d+)")


def enumerate_topic_ids(base: str) -> list[int]:
    """Read the sitemap index + child sitemaps -> sorted unique topic ids."""
    index = get_text(f"{base}/sitemap.xml")
    if not index:
        raise RuntimeError("could not fetch /sitemap.xml")
    child_sitemaps = re.findall(r"<loc>([^<]+)</loc>", index)
    if not child_sitemaps:
        raise RuntimeError("sitemap index had no child sitemaps")

    ids: set[int] = set()
    for sm in child_sitemaps:
        raw = get_text(sm)
        if not raw:
            print(f"  ! failed sitemap {sm}", file=sys.stderr)
            continue
        for loc in re.findall(r"<loc>([^<]+)</loc>", raw):
            m = TOPIC_URL_RE.search(urllib.parse.urlparse(loc).path)
            if m:
                ids.add(int(m.group(1)))
        print(f"  sitemap {sm.rsplit('/', 1)[-1]}: total unique topics so far {len(ids)}",
              file=sys.stderr)
        time.sleep(0.2)
    return sorted(ids)


# ---- per-topic post collection --------------------------------------------
def slim_post(p: dict) -> dict:
    return {k: p.get(k) for k in POST_FIELDS}


def fetch_all_posts(base: str, topic_id: int, topic: dict, batch: int, delay: float) -> list[dict]:
    """Return every post in a topic, fetching beyond the first chunk as needed."""
    stream = topic["post_stream"]["stream"]
    have = {p["id"]: p for p in topic["post_stream"]["posts"]}
    missing = [pid for pid in stream if pid not in have]

    for i in range(0, len(missing), batch):
        chunk = missing[i:i + batch]
        q = "&".join(f"post_ids[]={pid}" for pid in chunk)
        data = get_json(f"{base}/t/{topic_id}/posts.json?{q}")
        if data:
            for p in data["post_stream"]["posts"]:
                have[p["id"]] = p
        time.sleep(delay)

    # preserve forum order (by post_number), falling back to stream order
    ordered = [have[pid] for pid in stream if pid in have]
    ordered.sort(key=lambda p: p.get("post_number") or 0)
    return [slim_post(p) for p in ordered]


def scrape_topic(base: str, topic_id: int, batch: int, delay: float) -> dict | None:
    topic = get_json(f"{base}/t/{topic_id}.json")
    if not topic or "post_stream" not in topic:
        return None
    posts = fetch_all_posts(base, topic_id, topic, batch, delay)
    return {
        "topic_id": topic.get("id"),
        "title": topic.get("title"),
        "slug": topic.get("slug"),
        "category_id": topic.get("category_id"),
        "tags": topic.get("tags"),
        "created_at": topic.get("created_at"),
        "last_posted_at": topic.get("last_posted_at"),
        "views": topic.get("views"),
        "like_count": topic.get("like_count"),
        "posts_count": topic.get("posts_count"),
        "url": f"{base}/t/{topic.get('slug')}/{topic.get('id')}",
        "posts": posts,
    }


def build_contacts(base: str, rec: dict) -> list[dict]:
    """One record per post that contains an email, with full post+topic context."""
    contacts = []
    for p in rec["posts"]:
        emails = extract_emails(p.get("cooked") or "")
        if not emails:
            continue
        contacts.append({
            "emails": emails,
            "topic_id": rec["topic_id"],
            "topic_title": rec["title"],
            "topic_url": rec["url"],
            "category_id": rec["category_id"],
            "post_id": p["id"],
            "post_number": p["post_number"],
            "post_url": f"{base}/t/{rec['slug']}/{rec['topic_id']}/{p['post_number']}",
            "user_id": p["user_id"],
            "username": p["username"],
            "name": p["name"],
            "created_at": p["created_at"],
            "cooked": p["cooked"],
        })
    return contacts


# ---- main -----------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape all posts from a Discourse forum.")
    default_dir = SCRIPT_DIR / DEFAULT_SITE
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"forum base URL (default: {DEFAULT_BASE})")
    ap.add_argument("--out", default=str(default_dir / f"{DEFAULT_SITE}_posts.jsonl"),
                    help="output JSONL file (all posts)")
    ap.add_argument("--emails-out", default=str(default_dir / f"{DEFAULT_SITE}_emails.jsonl"),
                    help="output JSONL file (only posts that contain an email)")
    ap.add_argument("--limit", type=int, default=0, help="max topics to scrape (0 = all)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests (default: 0.5)")
    ap.add_argument("--batch", type=int, default=50, help="post ids per batch request (default: 50)")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # Make sure the output directory exists (e.g. threejs/).
    for path in (args.out, args.emails_out):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    print(f"Enumerating topics from {base}/sitemap.xml ...", file=sys.stderr)
    topic_ids = enumerate_topic_ids(base)
    print(f"Found {len(topic_ids)} topics.", file=sys.stderr)

    done = load_done_keys(args.out, "topic_id")
    if done:
        print(f"Resuming: {len(done)} topics already in {args.out}, skipping them.", file=sys.stderr)
    todo = [tid for tid in topic_ids if tid not in done]
    if args.limit > 0:
        todo = todo[:args.limit]
    print(f"Scraping {len(todo)} topics -> {args.out}", file=sys.stderr)

    scraped = posts_total = contacts_total = 0
    with open(args.out, "a", encoding="utf-8") as out, \
            open(args.emails_out, "a", encoding="utf-8") as eout:
        for i, tid in enumerate(todo, 1):
            try:
                rec = scrape_topic(base, tid, args.batch, args.delay)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(todo)}] ! topic {tid}: {e}", file=sys.stderr)
                time.sleep(2)
                continue
            if not rec:
                print(f"  [{i}/{len(todo)}] - topic {tid}: skipped (deleted/empty)", file=sys.stderr)
                continue
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            scraped += 1
            posts_total += len(rec["posts"])

            contacts = build_contacts(base, rec)
            for c in contacts:
                eout.write(json.dumps(c, ensure_ascii=False) + "\n")
            if contacts:
                eout.flush()
                contacts_total += len(contacts)
            email_note = f" | {len(contacts)} w/ email" if contacts else ""
            print(f"  [{i}/{len(todo)}] + topic {tid}: {len(rec['posts'])} posts{email_note} "
                  f"| {rec['title'][:50]}", file=sys.stderr)
            time.sleep(args.delay)

    print(f"\nDone. Scraped {scraped} topics, {posts_total} posts this run.", file=sys.stderr)
    print(f"  all posts   -> {args.out}", file=sys.stderr)
    print(f"  {contacts_total} posts with email(s) -> {args.emails_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
