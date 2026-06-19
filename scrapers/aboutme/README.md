# about.me profile scraper

Scrapes public [about.me](https://about.me) user profiles into structured JSON.
Shares the HTTP, email, and JSONL helpers in [`../common/`](../common).

## How it works

about.me publishes every public profile in its sitemap, and each profile page
embeds the full profile object as JSON in a
`<script type="text/json" class="contextData">` tag. The scraper walks:

```
robots.txt → SitemapIndex.xml → ~225 SitemapUser_*.xml (20k URLs each, ~4.5M total)
            → https://about.me/<username> → contextData JSON
```

No external dependencies — Python 3 standard library only.

## Usage

```bash
# scrape everything (~4.5M profiles — long-running, resumable) — this is the default.
# ONLY users whose content contains an email are kept.
python scrapers/aboutme/aboutme_scrape.py

# also skip anyone located in Korea
python scrapers/aboutme/aboutme_scrape.py --exclude-location=kr

# cap the run if you want a quick sample
python scrapers/aboutme/aboutme_scrape.py --exclude-location=kr --limit 100

# resume a huge run from a given child sitemap
python scrapers/aboutme/aboutme_scrape.py --start-sitemap SitemapUser_g.xml
```

Output is appended to `users.jsonl` (one user per line, **resumable** — re-running
skips usernames already written). A pretty `users.json` array is also written when
the set is small enough (< 100k records).

| flag | default | meaning |
|------|---------|---------|
| `--limit` | `0` | max profiles to keep (`0` = no limit) |
| `--out` | `users.jsonl` | resumable JSONL output |
| `--json-out` | `users.json` | pretty JSON array (small sets only) |
| `--delay` | `0.3` | seconds between requests (be polite) |
| `--users` | – | comma-separated usernames instead of the sitemap |
| `--start-sitemap` | – | resume enumeration from a child sitemap filename |
| `--exclude-location` | – | skip matching locations (`kr` → Korean place names) |

Profiles are **always** filtered to those whose content (bio + links) contains an
email — that is the whole point of this scraper. `--limit N` counts **kept**
(email) users. Email is rare on about.me (see below), so a full run scans
millions of profiles to collect the matches.

`--exclude-location=kr` expands to Korean place names (`korea, seoul, busan,
incheon, daegu, gwangju, daejeon, suwon, ulsan` + Hangul `한국 / 서울 / 대한민국`),
since about.me locations are free text with no country code. You can also pass
your own terms, e.g. `--exclude-location=korea,japan,china`.

## Important: age, gender, and email are not published by about.me

about.me profiles **do not contain** `age`, `gender`, or `email` fields anywhere
— there is simply no such data on a profile. Contact is handled through an on-site
form (`contact_me`), so email addresses are deliberately hidden.

These fields are still emitted to satisfy the requested schema:

- `age`, `gender` → always `null`.
- `email` / `emails` → best-effort only: the scraper scans the user's own bio and
  links text for an email they may have typed in. Most profiles will be `null`.

Everything else (name, role, location, summary, schools, interests, tags, links)
is taken directly from the profile and is reliably populated when the user
filled it in.
