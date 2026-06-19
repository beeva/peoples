# dev.to job-post scraper

`devto_scrape.py` scrapes job posts on dev.to (articles tagged `#hiring`,
`#forhire`, `#jobs`) for contact info. The Forem (dev.to) public API exposes
each article's full `body_markdown`, which is scanned for emails, `mailto:`
links, ATS / careers links, and messaging links (Telegram / Discord).

Shares the HTTP and email helpers in [`../common/`](../common); the apply-link
and messaging extractors are dev.to-specific.

## Usage

```bash
python scrapers/devto/devto_scrape.py
# → writes scrapers/devto/jobs.json
```

| flag         | default               | meaning                          |
| ------------ | --------------------- | -------------------------------- |
| `--tags`     | `hiring,forhire,jobs` | comma-separated dev.to tags      |
| `--pages`    | `3`                   | pages per tag                    |
| `--per-page` | `50`                  | articles per page                |
| `--out`      | `jobs.json`           | output JSON file                 |

No external dependencies — Python 3 standard library only.

## Output schema

```json
{
  "id": 123,
  "title": "Senior Frontend Engineer",
  "url": "https://dev.to/...",
  "published_at": "2026-01-01T00:00:00Z",
  "author": "Jane Doe",
  "organization": "Acme",
  "tags": ["hiring", "react"],
  "reading_time_minutes": 2,
  "contact": {
    "emails": ["jobs@acme.com"],
    "mailto": ["jobs@acme.com"],
    "apply_links": ["https://acme.greenhouse.io/..."],
    "messaging": ["https://t.me/acmejobs"]
  },
  "description": "<full body_markdown>"
}
```

Only posts that contain at least one contact channel are emitted.
