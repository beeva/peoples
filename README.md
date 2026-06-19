# email-scrapper

A small monorepo for **scraping public contact emails** from a few sources and
**browsing** the three.js results through a polished UI.

Everything is Python **standard library only** (no pip installs) on the scraping
side; the web frontend is a Next.js app.

## Layout

```
scrapers/
  common/                      # shared engine: HTTP retry/back-off, email
    http.py                    #   extraction, resumable JSONL helpers
    emails.py
    jsonl.py
  devto/                       # dev.to job-post contact scraper
    devto_scrape.py            #   -> jobs.json
  discourse/                   # reusable Discourse forum scraper
    discourse_scrape.py
    threejs/                   #   one scraped site (discourse.threejs.org)
      threejs_emails.jsonl     #   posts containing an email (served below)
      threejs_posts.jsonl      #   every post (created on full run)
  aboutme/                     # about.me public-profile scraper
    aboutme_scrape.py          #   -> users.jsonl / users.json

server.py                      # zero-dependency data API + standalone UI
index.html                     #   the standalone single-page UI (served by server.py)
web/                           # Next.js (App Router) frontend
package.json                   # orchestrates server.py + web/ together
```

The three scrapers are independent but share `scrapers/common/`, so each one is
just its site-specific glue (URL enumeration + record shape) on top of the
common HTTP / email / JSONL machinery.

## Scrapers

Each scraper runs standalone and writes its output next to itself. See the
per-scraper notes in [`scrapers/discourse/README.md`](scrapers/discourse/README.md).

```bash
# three.js forum -> scrapers/discourse/threejs/threejs_{posts,emails}.jsonl
python scrapers/discourse/discourse_scrape.py

# dev.to job posts -> scrapers/devto/jobs.json
python scrapers/devto/devto_scrape.py

# about.me profiles -> scrapers/aboutme/users.jsonl
python scrapers/aboutme/aboutme_scrape.py --limit 100
```

All three are **resumable** (re-running skips records already written) and
**polite** (retry + back-off, honouring `Retry-After`).

## Browsing the contacts

The data server loads all three datasets, normalises them into one record shape
(tagged with a `source`), and exposes them together with a category selector:

| source      | file                                              |
| ----------- | ------------------------------------------------- |
| `discourse` | `scrapers/discourse/threejs/threejs_emails.jsonl` |
| `devto`     | `scrapers/devto/jobs.json`                         |
| `aboutme`   | `scrapers/aboutme/users.jsonl`                     |

It exposes them two ways:

- **`server.py`** — a zero-dependency JSON API (`/api/emails`, `/api/stats`)
  that also serves the standalone `index.html` UI. Run on its own with no
  Node.js required.
- **`web/`** — a richer Next.js frontend that fetches from the same API.

Both frontends show a tab bar to filter by source (**All / three.js forum /
dev.to jobs / about.me**) and adapt each card to the source (forum topics, job
posts with apply/messaging links + tags, or profiles with role/location).

### API

```
GET /api/emails?source=all|discourse|devto|aboutme&q=&sort=newest|oldest&page=&per_page=
GET /api/stats?source=...
```

`source` defaults to `all`. Each response also returns the `sources` list (with
per-source counts) used to render the selector.

### Prerequisites

- Python 3.9+
- Node.js 18+ (only for the `web/` frontend)

### Run everything (one command)

Installs root tooling (`concurrently`) and the web app's deps:

```bash
npm run setup
```

Starts the Python data server **and** the Next.js app together:

```bash
npm run dev      # development: Next dev server with hot reload
# or
npm run build && npm run start    # production
```

| Service        | URL                     |
| -------------- | ----------------------- |
| Web frontend   | http://localhost:3000   |
| Data server    | http://127.0.0.1:8000   |

`Ctrl+C` stops both (processes are linked with `concurrently -k`).

### Zero-dependency mode (no Node.js)

Just run the Python server and open the standalone UI it serves:

```bash
python server.py            # http://127.0.0.1:8000
python server.py 9000       # custom port
```

### Useful individual scripts

| Script               | What it does                    |
| -------------------- | ------------------------------- |
| `npm run dev:server` | Python data server only         |
| `npm run dev:web`    | Next.js dev server only         |
| `npm run build`      | Production build of the web app |

## Configuration

The frontend reads `API_BASE_URL` (default `http://127.0.0.1:8000`). To point at
a different backend, copy `web/.env.example` to `web/.env.local` and edit it.
