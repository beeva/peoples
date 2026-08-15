# peoples

A small monorepo for **scraping public contact emails** from a few sources and
**browsing** the three.js results through a polished UI.

Everything is stored in **MySQL**, which belongs to the project: its data
directory, its config and its port all live in `db/`, and `npm run dev` starts
it alongside the API and the web app. There is no XAMPP control panel step, and
no data files — the scrapers insert straight into the database.

## Layout

```
db/
  my.ini                       # this project's MySQL config (committed)
  data/                        # its data directory (created on first run)

scrapers/
  common/                      # shared engine: HTTP retry/back-off, email and
    http.py                    #   phone extraction, and the DB write handle
    emails.py
    phones.py                  #   phone / WhatsApp numbers -- E.164, strict
    store.py                   #   RecordStore / SkipStore -- straight to MySQL
  devto/       devto_scrape.py       # dev.to job-post contacts
  discourse/   discourse_scrape.py   # reusable Discourse forum scraper
  aboutme/     aboutme_scrape.py     # about.me public profiles
  github/      github_scrape.py      # GitHub profiles + portfolio sites
               regions.py            #   US / Europe / South America filter
  phone_pass.py                # revisit every source's sites for numbers

server.py                      # JSON API: HTTP routing, scrape jobs, Claude, SMTP
db.py                          #   MySQL connection + schema (and its migrations)
records.py                     #   raw scraped dict -> one common record shape
dbsync.py                      #   the write path: store + merge into contacts
dbquery.py                     #   the read side: list/detail/export/facets, as SQL
dbphones.py                    #   phones: backfill from records, revalidate
dbdump.py                      #   export the database to .sql, and restore one
dbslack.py                     #   Slack workspace exports
scripts/
  mysql-server.js              # starts/stops this project's MySQL
  kill-port.js
  run_scraper.py               # runs one scraper by name (used by CI)
web/                           # Next.js (App Router) frontend
  middleware.ts                #   HTTP Basic auth, when UI_USER/UI_PASS are set
package.json                   # orchestrates MySQL + server.py + web/ together

Dockerfile                     # the API as a container, for a managed host
DEPLOY.md                      # deploying the whole thing on free tiers
.github/workflows/             # scrape / backup / seed, on GitHub Actions
```

The scrapers are independent but share `scrapers/common/`, so each one is just
its site-specific glue (URL enumeration + record shape) on top of the common
HTTP / email / storage machinery.

## Scrapers

Each scraper runs standalone and writes into the database. See the per-scraper
notes in [`scrapers/discourse/README.md`](scrapers/discourse/README.md).

```bash
# MySQL has to be up; `npm run dev` does this for you, or:
npm run db:start

python scrapers/discourse/discourse_scrape.py
python scrapers/devto/devto_scrape.py
python scrapers/aboutme/aboutme_scrape.py --limit 100

# GitHub (US / Europe / South America only; wants a GITHUB_TOKEN in .env)
python scrapers/github/github_scrape.py --limit 100
```

All of them are **resumable** — they ask the database which records they
already hold, so re-running skips them — and **polite** (retry + back-off,
honouring `Retry-After`).

## Where the data lives

In MySQL. There are no data files.

```
scraper --INSERT--> records --merge--> contacts --SQL--> /api/emails --> web/
```

Each scraper stores a record the moment it collects it, and asks the database
what it already has instead of re-reading a file — so a run appears in the UI
while it is still going, and stopping one keeps everything it got. Every
request (filtering, sorting, paging, the facet counts, the CSV export) is a
query against indexed columns, so the cost of a page does not grow with the
size of the archive.

| table            | holds                                              | rebuildable?      |
| ---------------- | -------------------------------------------------- | ----------------- |
| `records`        | one scraped occurrence, **plus its original JSON** | **no** |
| `contact_phones` | phone / WhatsApp numbers per contact                | yes, from `records` |
| `contacts`       | merged people — what the list shows                 | yes, from `records` |
| `contact_emails` | every address, for the sent-log join                | yes, from `records` |
| `skipped`        | logins ruled out, so a rescrape skips them          | **no** |
| `slack_users`    | Slack workspace exports                             | **no** |
| `enrichment`     | Claude-inferred country + gender, by email          | **no** |
| `sent_log`       | who has been emailed                                | **no** |
| `app_state`      | per-source scraper state (cursors, last run)        | **no** |

**Nothing a scraper collects is thrown away.** `records.raw` keeps the
scraper's own dict verbatim next to the normalised columns, which are a lossy
projection of it (they have no room for `email_sources`, `followers`, `region`,
…). So the archive can be replayed into a different record shape without
re-scraping, and the database really is a complete replacement for the files.

**Merging by shared email happens once, on write.** The result is stored in
`contacts`, so the list never re-derives it. Country, gender and the messaged
counters are denormalised onto that row — and updated in place when an
inference or a send lands — so the filters and their facet counts can use an
index.

Merging is also *incremental*: storing a record rebuilds only the contacts that
record actually touches, which is what makes a per-record write affordable.

dev.to is scraped and stored but is not one of the tabs (it was dropped from
the listed sources before the database existed). "All" therefore means the
listed sources, not everything in the table.

Both frontends show a tab bar to filter by source (**All / three.js forum /
dev.to jobs / about.me / GitHub**) and adapt each card to the source (forum
topics, job posts with apply/messaging links + tags, or profiles with
role/location).

### API

```
GET  /api/emails?source=all|discourse|devto|aboutme|github&q=&sort=newest|oldest&page=&per_page=
GET  /api/stats?source=...
POST /api/scrape?source=discourse|devto|aboutme|github[&pages=&limit=&regions=&full=1]  # start a re-scrape
POST /api/scrape/stop?source=...                                         # stop a running job
GET  /api/scrape/status?source=...                                       # poll a job (live added count)
POST /api/runs/merge   {"source":"github","from":[5],"into":3}           # fold one step into another

GET  /api/db/status                          # server, size, table counts, sync state, dumps
GET  /api/db/export[?download=1&file=path]   # dump the database to .sql
POST /api/db/import                          # restore: raw .sql body, or {"path": "..."}
POST /api/db/import-files[?force=1]          # import pre-database data files, if any

GET  /api/emails?...&contactable=phone|whatsapp   # only contacts with a number
GET  /api/slack/workspaces                   # Slack workspaces + user counts
GET  /api/slack/users?ws=<slug>              # one workspace's users
```

`source` defaults to `all`. Each `/api/emails` response also returns the
`sources` list (with per-source counts) used to render the selector.

## Phone / WhatsApp numbers

Alongside the email address, contacts carry any phone numbers found for them.
**A contact needs a way to reach them, not an email specifically.** Every
scraper keeps a user with an email **or** a phone / WhatsApp number — someone
who published only a number is as contactable as someone who published only an
address. Contacts are also merged on a shared *number*, not just a shared
address, so the same person posting under two emails but one phone collapses to
one row.

A number is only kept when something vouches for it — a `wa.me` /
`api.whatsapp.com` link, a `tel:` link, a "WhatsApp:" / "Tel." label, or a
leading `+` and a real country calling code. A bare local number with no
country context is dropped, as are dates, version strings and order ids. That
trades recall for precision deliberately: a directory of numbers nobody can
call is worse than a smaller one that is right.

Numbers are stored in E.164 (`+447700900123`), so the same person written three
different ways de-duplicates to one contact. `whatsapp` records that a number
was published *as* a WhatsApp contact, not merely that it might work there, and
those sort first — it is a channel the person chose to be reachable on. The
**Reach** filter narrows the list to contacts with a number, or to those on
WhatsApp; both appear in the CSV export.

Finding them happens in two places, which cost very different amounts:

```bash
npm run db:phones      # re-extract from what is already stored -- seconds, free
npm run scrape:phones  # revisit contacts' own sites -- hours, thousands of requests
```

**A normal scrape already collects numbers.** The GitHub scraper fetches each
user's site and README for their email anyway, and now scans those same pages
for a phone in the same pass — same pages, same order, same early exit, so it
makes exactly as many requests as before. The other scrapers keep a post or
profile that offers a number even when it offers no address. `scrape:phones` is
therefore only for the **back catalogue**: contacts scraped before this existed,
whose pages were fetched and discarded. Once it has run, it never needs to again.

The first works because `records.raw` keeps each scraper's original JSON: new
kinds of extraction can be run over the whole archive without re-scraping
anything. The second exists because a profile almost never carries a number —
of 10,770 stored GitHub profiles the bio yielded two — while a developer's own
site often does, in a footer or a contact page. Every source stores somewhere
to go back to, so the pass covers all of them:

| source      | where it goes back to                                    |
| ----------- | -------------------------------------------------------- |
| `github`    | the profile's `blog` field, plus the `<login>/<login>` README |
| `aboutme`   | the profile's own links, and any URL in its summary       |
| `discourse` | sites the person linked from their posts                  |
| `devto`     | the post's apply / messaging links                        |

Each site is crawled homepage-first, then `/contact` and `/about`, stopping as
soon as a WhatsApp link turns up. Profile silos (LinkedIn, Twitter, …) and
asset URLs are skipped — the request costs as much as a useful one. The pass is
resumable (visited records are remembered, per source), safe to stop (each hit
is stored as it is found), and throttled like the scrapers.

```bash
npm run scrape:phones -- --source aboutme   # one source
npm run scrape:phones -- --limit 200        # per source, for a taste of it
npm run scrape:phones -- --all              # revisit contacts that already have one
```

## Re-scraping from the UI

Selecting a single source (any tab except **All**) reveals a **Rescrape**
button. It runs that source's scraper as a background job on the data server
and streams progress.

- **Real-time updates** — the scraper stores each record as it collects it, so
  new contacts (and the running `+N new` count) are already in the database
  before the next profile is fetched; the UI refreshes every ~2s to pick them
  up. There is no file to re-read, so this costs the same however large the
  archive gets.
- **Stop** — a **Stop** button appears while scraping. It terminates the job
  but **keeps everything collected so far** (the run is marked `stopped`, not
  failed). Because each source is incremental, clicking **Rescrape** again
  simply continues from where it left off.

Every scrape is **incremental** — it only fetches what isn't already stored, so
re-scraping is cheap and safe to repeat:

| source      | resume checkpoint                          | how new content is found                                   |
| ----------- | ------------------------------------------ | ---------------------------------------------------------- |
| `discourse` | highest `topic_id` scraped (high-water mark) | re-scrapes only topics with `id >` the checkpoint; contacts also de-duped by stored `post_id` so a revisit never writes twice |
| `devto`     | article ids already stored                  | pages newest-first, stops once a page is fully known        |
| `aboutme`   | last sitemap walked + stored `username`s   | resumes from the saved sitemap cursor, skips known usernames |
| `github`    | location term in flight + stored `login`s  | resumes at the saved location term, skips known logins, and skips everyone in `skipped` |

**Checkpointing.** Each scrape advances its checkpoint after every item and
persists it to the `app_state` table via a per-source `.cursor` file. So re-scraping always continues **after** the last point — it never
re-fetches what's already saved, even across stops, restarts, or `--limit`-bounded
runs. The discourse checkpoint is seeded on first run from the highest `topic_id`
already in the data, so an initial re-scrape jumps straight to genuinely new
topics. (Pass `full=1` to dev.to, or clear the `app_state` table, to rebuild
from scratch.) UI-triggered runs are bounded
(`devto` → `--pages 3`, `aboutme` / `github` → `--limit 50`, `discourse` → `--limit 300`)
so each click returns promptly; click again to continue where it left off. Pass
`pages` / `limit` query params to override, or `full=1` (dev.to) to rebuild from
scratch. Run a scraper directly (see above) for an unbounded crawl.

### Merging steps

Every GitHub contact records the scrape run (**step**) that found it, and the
**Step** filter lists them. Two runs are often really one piece of work — a run
that stopped half way and the one that finished it — so the Step row has a
**Merge steps…** control: pick the run to fold away and the run to keep, confirm,
and every contact of the first is relabelled as the second.

Only the step label changes; no contact is added, removed or altered — it is an
`UPDATE` of the `run` column, not a rewrite of the archive. Merging is refused
while a scrape of that source is running, since that run is still handing out
its own step number.

## The database

### Prerequisites

- Python 3.9+ and `pip` (for PyMySQL — `npm run setup` installs it)
- Node.js 18+
- **XAMPP's MySQL**, already installed. Nothing needs to be running: the app
  starts and stops `mysqld` itself.
- A `GITHUB_TOKEN` in `.env` (only for the GitHub scraper — see
  [`scrapers/github/README.md`](scrapers/github/README.md); without one the API
  allows 60 requests/hour, which is too slow to be useful)

### Run everything (one command)

Installs PyMySQL, root tooling (`concurrently`) and the web app's deps:

```bash
npm run setup
```

Starts **MySQL, the data server and the Next.js app** together:

```bash
npm run dev      # development: Next dev server with hot reload
# or
npm run build && npm run start    # production
```

| Service        | URL                        |
| -------------- | -------------------------- |
| Web frontend   | http://localhost:3090      |
| Data server    | http://127.0.0.1:8000      |
| MySQL          | 127.0.0.1:3306             |

`Ctrl+C` stops all three (processes are linked with `concurrently -k`), and
MySQL is asked to shut down cleanly so the next start skips InnoDB crash
recovery.

One Windows caveat: `mysqld` is a child of the helper script, and if that
script is terminated abruptly — which is what happens when `concurrently -k`
tears the group down because *another* process died — the database can be left
running. Nothing breaks (the next `npm run dev` finds it and reuses it), but if
you want it actually stopped:

```bash
npm run db:stop     # just MySQL
npm run kill        # data server + web app + MySQL
```

On the very first run `db/data` is created (with MariaDB's own bootstrap tool)
and the schema is installed. Any pre-database data files still lying around are
imported once; after that there is nothing to check at startup.

**The database is the project's, not XAMPP's.** It runs from `db/data` with
`db/my.ini` on **port 3307**, so it can sit beside whatever XAMPP has on 3306
and neither can see the other's databases. XAMPP supplies only `mysqld.exe`.
That also means the tuning is ours: `db/my.ini` sets a 256MB buffer pool and a
64MB packet limit, where XAMPP's stock config (tuned for a 64MB machine) would
spend a bulk load thrashing index pages to disk.

If something is already listening on 3307 it is reused and **not** shut down on
exit — it is not ours to stop. The server checks that whatever answered is
really serving `db/data` and says so loudly if it is not, so a stray MySQL on
that port cannot quietly look like an empty archive.

### Export / import .sql

The **Database** page in the UI (sidebar → System → Database) shows the server,
the per-table row counts and what each source holds, and has buttons to export
and import. The same from a terminal:

```bash
npm run db:export                 # -> backups/email_scrapper-<timestamp>.sql
npm run db:export -- my-dump.sql  # a specific path
npm run db:import -- my-dump.sql  # restore (replaces everything)
npm run db:status                 # server, table counts, per-source counts
```

An export is an ordinary `mysqldump`, so it also restores through phpMyAdmin, a
plain `mysql <`, or onto another MySQL server. `backups/` is git-ignored.

### Other database scripts

| Script                | What it does                                             |
| --------------------- | -------------------------------------------------------- |
| `npm run db:start`    | Start MySQL on its own (stays attached)                   |
| `npm run db:stop`     | Shut it down cleanly                                      |
| `npm run scrape:phones` | Revisit every contact's own site looking for phone numbers |
| `npm run db:phones`   | Re-extract phones from stored records (no network)         |
| `npm run db:phones:status` | How many contacts have a number                      |
| `npm run db:phones:revalidate` | Drop stored numbers the current rules reject      |
| `npm run db:rebuild-contacts` | Recompute the merged contacts from `records`      |
| `npm run db:sync`     | Import pre-database data files, if any are still on disk   |
| `npm run db:rebuild`  | Re-import them from the start                              |
| `npm run dev:server`  | Data server only (expects MySQL to be up)                  |
| `npm run dev:web`     | Next.js dev server only                                    |
| `npm run build`       | Production build of the web app                            |

The two import commands are no-ops on a migrated setup — scrapers write to the
database directly, so there are no files to read. They exist for an archive
collected before the move.

To move the whole thing to another machine, copy `backups/*.sql` (or the
`db/data` folder with MySQL stopped) — that is the entire archive.

## Configuration

Copy `.env.example` to `.env`. The database settings there are
`MYSQL_BASEDIR` (where to find `mysqld.exe` — `D:/xampp/mysql`,
`C:/xampp/mysql` and `E:/xampp/mysql` are tried when it is blank),
`MYSQL_DATADIR` (blank = `db/data`), `MYSQL_HOST` / `MYSQL_PORT` (3307) /
`MYSQL_USER` / `MYSQL_PASSWORD` (the project's own `root`, no password),
`MYSQL_DATABASE` (default `email_scrapper`, created if missing) and
`MYSQL_BACKUP_DIR`.

The frontend reads `API_BASE_URL` (default `http://127.0.0.1:8000`). To point at
a different backend, copy `web/.env.example` to `web/.env.local` and edit it.

## Deployment

Three guides, by what you need:

- **[VERCEL.md](VERCEL.md)** — the deployment this project runs: two Vercel
  projects (the app and the API) against managed MySQL, with scrapes on GitHub
  Actions. Start here.
- **[DEPLOY.md](DEPLOY.md)** — the wider picture: what else the stack could run
  on, what each host costs you, and why.
- **[CREDENTIALS.md](CREDENTIALS.md)** — where every key and token comes from,
  and which of the three places each one has to be pasted into.

The code for it is here and every part of it is **off by default**, so nothing
below changes how the project runs locally:

| set this | and | |
| --- | --- | --- |
| `HOST` / `PORT` | the API binds a public interface instead of loopback | `server.py` |
| `MYSQL_SSL=1` | it connects to a managed database over TLS | `db.py`, `dbdump.py` |
| `API_TOKEN` | every request must carry `X-Api-Token` | `server.py` |
| `UI_USER` / `UI_PASS` | HTTP Basic auth over the whole UI | `web/middleware.ts` |
| `GH_REPO` / `GH_DISPATCH_TOKEN` | Rescrape dispatches a GitHub Actions run instead of forking a subprocess | `server.py` |

The last one is there because a scrape takes hours and a free container does
not. Dispatched runs are built from the same arguments a local scrape would
use, so filtering the list and pressing Rescrape works the same either way.
