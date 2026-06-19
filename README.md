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
GET  /api/emails?source=all|discourse|devto|aboutme&q=&sort=newest|oldest&page=&per_page=
GET  /api/stats?source=...
POST /api/scrape?source=discourse|devto|aboutme[&pages=&limit=&full=1]   # start a re-scrape
POST /api/scrape/stop?source=...                                         # stop a running job
GET  /api/scrape/status?source=...                                       # poll a job (live added count)
```

`source` defaults to `all`. Each `/api/emails` response also returns the
`sources` list (with per-source counts) used to render the selector.

## Re-scraping from the UI

Selecting a single source (any tab except **All**) reveals a **Rescrape**
button. It runs that source's scraper as a background job on the data server
and streams progress.

- **Real-time updates** — the server re-reads the source from disk every ~2s
  while the scrape runs, and the UI refreshes on the same cadence, so new
  records (and the running `+N new` count) appear live instead of only at the
  end. The scrapers flush incrementally (dev.to writes atomically after each
  hit), so what you see is always consistent.
- **Stop** — a **Stop** button appears while scraping. It terminates the job
  but **keeps everything collected so far** (the run is marked `stopped`, not
  failed). Because each source is incremental, clicking **Rescrape** again
  simply continues from where it left off.

Every scrape is **incremental** — it only fetches what isn't already stored, so
re-scraping is cheap and safe to repeat:

| source      | resume checkpoint                          | how new content is found                                   |
| ----------- | ------------------------------------------ | ---------------------------------------------------------- |
| `discourse` | highest `topic_id` scraped (high-water mark) | re-scrapes only topics with `id >` the checkpoint; contacts also de-duped by `post_id` so a revisit never writes twice |
| `devto`     | article `id`s in `jobs.json`               | pages newest-first, stops once a page is fully known        |
| `aboutme`   | last sitemap walked + saved `username`s    | resumes from the saved sitemap cursor, skips known usernames |

**Checkpointing.** Each scrape advances its checkpoint after every item and
persists it to `scrapers/state.json` (git-ignored) via a per-source `.cursor`
file. So re-scraping always continues **after** the last point — it never
re-fetches what's already saved, even across stops, restarts, or `--limit`-bounded
runs. The discourse checkpoint is seeded on first run from the highest `topic_id`
already in the data, so an initial re-scrape jumps straight to genuinely new
topics. (Pass `full=1` to dev.to, or delete `state.json`, to rebuild from scratch.) UI-triggered runs are bounded
(`devto` → `--pages 3`, `aboutme` → `--limit 50`, `discourse` → `--limit 300`)
so each click returns promptly; click again to continue where it left off. Pass
`pages` / `limit` query params to override, or `full=1` (dev.to) to rebuild from
scratch. Run a scraper directly (see above) for an unbounded crawl.

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
