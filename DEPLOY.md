# Deploying this project — free, fully managed, no machines

No VM, no home server, no always-on PC. Everything runs on free tiers of
managed platforms, deployed from the GitHub repo, with nothing for you to
administer.

**The code for this is already in the repo.** Nothing below asks you to edit a
file — deployment is configuration now, not patches. What each setting does,
and what it costs you, is spelled out as it comes up.

**Need the actual credentials?** [CREDENTIALS.md](CREDENTIALS.md) is the
companion: where to click for every key and token, what to paste it into, and
how to prove it works.

---

## Contents

1. [What "no machines" costs you](#1-what-no-machines-costs-you)
2. [The architecture](#2-the-architecture)
3. [Pick your providers](#3-pick-your-providers)
4. [What is already implemented](#4-what-is-already-implemented)
5. [Step 1 — The database](#step-1--the-database)
6. [Step 2 — Load your data into it](#step-2--load-your-data-into-it)
7. [Step 3 — The API](#step-3--the-api)
8. [Step 4 — The frontend](#step-4--the-frontend)
9. [Step 5 — The scrapers, on GitHub Actions](#step-5--the-scrapers-on-github-actions)
10. [Step 6 — Reconnect the Rescrape button](#step-6--reconnect-the-rescrape-button)
11. [Alternative — everything on Vercel](#11-alternative--everything-on-vercel)
12. [Cold starts, sleeping, and keeping it awake](#12-cold-starts-sleeping-and-keeping-it-awake)
13. [Security checklist](#13-security-checklist)
14. [When free runs out](#14-when-free-runs-out)
15. [Verification](#15-verification)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What "no machines" costs you

Three real consequences. All three have answers below.

**1. Scrapes move to GitHub Actions.** `server.py` starts a scraper with
`subprocess.Popen` and streams its output for as long as the job runs — a phone
pass takes hours across thousands of requests. Every free managed host either
sleeps on idle, caps request duration, or discards the container's filesystem,
so a scrape started there dies part-way and wastes every request it made.
[Step 5](#step-5--the-scrapers-on-github-actions) moves scrapes to Actions,
which is free and is also not a machine you own.
[Step 6](#step-6--reconnect-the-rescrape-button) makes the UI's Rescrape button
dispatch one, so nothing is lost from the interface.

**2. The database stops being local, and queries slow down.** Today MySQL is a
local socket and a list page takes ~34 ms. Managed MySQL is a network hop. The
mitigation is not a bigger instance — it is **putting the API in the same region
as the database**. Get that right and it stays fast; get it wrong and every page
pays a cross-Atlantic round-trip.

**3. Enrichment needs a container, not a serverless function.** `server.py`
starts background threads that infer country and gender via Claude. Threads do
not survive on a serverless function — the invocation ends and the thread is
killed mid-flight. On a container host they run exactly as they do now. That is
why [Step 3](#step-3--the-api) recommends a container over
[the all-Vercel alternative](#11-alternative--everything-on-vercel).

You keep everything else: the UI, all filtering and faceting, CSV export,
contact detail, message generation, SMTP sending, the phone/WhatsApp work, and
the full 12,000+ contact archive.

---

## 2. The architecture

```
                    ┌──────────────────────────────┐
   your browser ───▶│  Vercel — Next.js frontend   │   free, HTTPS, Basic auth
                    │  web/  (the only public URL) │
                    └───────────────┬──────────────┘
                                    │  server-side fetch only,
                                    │  X-Api-Token header
                                    ▼
                    ┌──────────────────────────────┐
                    │  Vercel / Koyeb / Render     │   free
                    │  server.py — the JSON API    │
                    └───────────────┬──────────────┘
                                    │  MySQL over TLS
                                    ▼
                    ┌──────────────────────────────┐
                    │  Aiven or TiDB Cloud         │   free managed MySQL
                    │  email_scrapper (~140 MB)    │
                    └───────────────▲──────────────┘
                                    │
                    ┌───────────────┴──────────────┐
                    │  GitHub Actions — scrapers   │   free, scheduled, 6 h cap
                    │  .github/workflows/scrape.yml│
                    └──────────────────────────────┘
```

The structural fact that makes this safe: **the browser never talks to the
Python API.** Every call goes through the Next.js server — server components and
the route handlers in `web/app/api/*`, all of which read `API_BASE_URL`
(`web/lib/emails.ts`). There is no CORS to configure, the API's address is never
sent to the browser, and the shared token stays server-side.

---

## 3. Pick your providers

| layer | recommended | alternatives |
| --- | --- | --- |
| **Database** | **Aiven for MySQL** — free plan, 1 CPU / 1 GB / 5 GB disk | TiDB Cloud Starter (25 GB, MySQL-compatible) |
| **API** | **Vercel** (a second project — see VERCEL.md) or **Koyeb**'s free instance | Render free (sleeps after 15 min idle) |
| **Frontend** | **Vercel Hobby** | Cloudflare Pages, Netlify |
| **Scrapers** | **GitHub Actions** | — |

Three provider notes that will save you an afternoon:

- **PlanetScale removed its free tier** in 2024. **Railway** is trial-credit,
  not free. **Clever Cloud's** free MySQL is 10 MB — about 1/14th of what you
  need.
- **Hugging Face Spaces looks like a fit and was not.** Tried and abandoned: a
  free account is allocated *zero* CPU quota, so a Space accepts the code and
  the secrets, then refuses to start with `Quota exceeded for flavor cpu-basic
  (requested=1): current=0, limit=0`. It is not about the Space being private —
  making it public changed nothing — and not about contention, since the
  account owned exactly one paused Space. Running one needs PRO.
- **Do not switch to free Postgres** (Neon, Supabase) to chase a bigger free
  tier. This project is MySQL all the way down: PyMySQL, `mysqldump`-based
  export/import, backtick-quoted DDL, `INSERT … ON DUPLICATE KEY UPDATE`,
  `MEDIUMTEXT`. Porting it is a week of work and a permanent maintenance tax.
- **Vercel Hobby is licensed for non-commercial use.** If this directory feeds
  commercial outreach, use **Cloudflare Pages** or **Netlify** — both free, both
  run Next.js 15, neither has that clause.

Free tiers change; confirm current limits before committing.

---

## 4. What is already implemented

All of it is off by default, so local development is exactly as it was — the
server still binds `127.0.0.1`, still runs scrapes in-process, still connects to
the local database without TLS, and the UI still has no login. Each feature
turns on when you set its variable.

| capability | where | turned on by |
| --- | --- | --- |
| Bind a public interface, take the platform's port | `server.py` | `HOST=0.0.0.0`, `PORT` |
| TLS to the database | `db.py` (`_ssl_options`) | `MYSQL_SSL=1` or `MYSQL_SSL_CA=` |
| TLS for `mysqldump`/`mysql` too | `dbdump.py` (`_ssl_flags`) | the same two |
| Tolerate a database you may not create | `db.py` (`ensure_database`) | automatic |
| Skip the local-datadir check on a remote host | `db.py` (`_check_datadir`) | automatic |
| Shared-secret auth on every request | `server.py` (`Handler._authorised`) | `API_TOKEN` |
| Send that token from the web app | `web/lib/emails.ts` (`API_HEADERS`), all 16 call sites | `API_TOKEN` |
| Basic auth over the whole UI | `web/middleware.ts` | `UI_USER`, `UI_PASS` |
| Dispatch scrapes to GitHub Actions | `server.py` (`_dispatch_scrape`) | `GH_REPO`, `GH_DISPATCH_TOKEN` |
| Container image for the API | `Dockerfile`, `.dockerignore` | — |
| Scrape / backup / seed workflows | `.github/workflows/` | repository secrets |

Two details worth knowing because they are load-bearing:

**The token comparison is constant-time.** `Handler._authorised` uses
`hmac.compare_digest`, so a wrong token cannot be narrowed down by timing the
rejection.

**A remote scrape is targeted by the same filters as a local one.**
`_remote_args` builds the arguments with the very same `_scrape_argv` the local
path uses, joins them with `shlex.quote`, and `scripts/run_scraper.py` splits
them back with `shlex.split`. So "filter the list, then Rescrape" keeps working
on a runner, and a country name with a space in it survives the trip.

The full list of variables, with comments, is in `.env.example` (API) and
`web/.env.example` (frontend).

---

## Step 1 — The database

### 1.1 Create it (Aiven)

1. Sign up at [console.aiven.io](https://console.aiven.io) — no card needed for
   the free plan.
2. **Create service → MySQL → Free plan.**
3. **Pick the region now and write it down.** The API host must match it. This
   is the single biggest performance decision in the deployment.
4. Wait for *Running* (a few minutes).

From **Overview**, collect: host, port, user (`avnadmin`), password, database
name (`defaultdb`), and download the **CA certificate**.

Aiven gives you `defaultdb`. Either use it (`MYSQL_DATABASE=defaultdb`) or make
the project's own from the console's Query editor:

```sql
CREATE DATABASE email_scrapper
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 1.2 Raise the packet limit

A restore sends large statements; the default 1 MB fails part-way. **Service
settings → Advanced configuration → `mysql.max_allowed_packet` → `67108864`**
(64 MB). Apply and let it restart.

### 1.3 The connection settings

```
MYSQL_HOST=mysql-xxxxxxx-yourproject.a.aivencloud.com
MYSQL_PORT=12345
MYSQL_USER=avnadmin
MYSQL_PASSWORD=AVNS_...
MYSQL_DATABASE=email_scrapper
MYSQL_SSL=1
```

`MYSQL_SSL=1` trusts the system certificate store, which works because Aiven's
certificate chains to a public root. If a host's image has no CA bundle, commit
the downloaded `ca.pem` and set `MYSQL_SSL_CA=/app/ca.pem` instead.

### TiDB Cloud instead

Same shape: 25 GB free rather than 5 GB, and MySQL-compatible rather than MySQL.
Create a **Starter** cluster, take the connection string, set `MYSQL_SSL=1`.
Before trusting it, finish [Step 2](#step-2--load-your-data-into-it) and confirm
the row counts match — a full restore is where any incompatibility shows up.

---

## Step 2 — Load your data into it

Your archive is ~360 MB of MySQL data directory, ~140 MB of tables, and dumps to
a ~52 MB `.sql`. Never copy `db/data` — InnoDB tablespaces are not portable
between servers. Always move a dump.

### 2.1 A dialect warning, because it will bite you otherwise

**Your local server is MariaDB 10.4** (XAMPP ships MariaDB under the `mysql`
name), and Aiven serves **MySQL 8**. `npm run db:export` therefore writes a
*MariaDB* dump. In practice the schema here is plain enough to cross over —
InnoDB, `utf8mb4`, no stored routines, no triggers, no MariaDB-only column types
— and a straight restore normally works.

If it does not, do not fight the dump. Let the project create its own schema and
import **data only**, which sidesteps every DDL dialect difference:

```powershell
# 1. Let the server create the schema on the target, from db.py's own portable
#    DDL. Point .env at the managed database first.
python -c "import db; db.bootstrap()"

# 2. Dump data only, no CREATE TABLE at all.
& "D:\xampp\mysql\bin\mysqldump.exe" --host=127.0.0.1 --port=3307 --user=root `
    --no-create-info --skip-triggers --single-transaction --quick `
    --default-character-set=utf8mb4 --max-allowed-packet=64M `
    email_scrapper > backups\data-only.sql

# 3. Load it.
& "D:\xampp\mysql\bin\mysql.exe" --host=<aiven-host> --port=<port> `
    --user=avnadmin --password --ssl-mode=REQUIRED `
    --max-allowed-packet=64M email_scrapper < backups\data-only.sql
```

### 2.2 The normal path

```powershell
npm run db:start
npm run db:export        # -> backups/email_scrapper-<date>.sql
```

Then load it, by whichever route suits you:

**A. From your PC**, with the XAMPP client you already have:

```powershell
& "D:\xampp\mysql\bin\mysql.exe" `
    --host=mysql-xxxxxxx.aivencloud.com --port=12345 `
    --user=avnadmin --password --ssl-mode=REQUIRED `
    --max-allowed-packet=64M `
    email_scrapper < backups\email_scrapper-2026-08-11.sql
```

**B. Through the project** — put the Aiven settings in `.env` (including
`MYSQL_SSL=1`) and run:

```bash
python dbdump.py import backups/email_scrapper-2026-08-11.sql
```

`dbdump.py` now asks the client binary which TLS options it understands and
passes the right ones, so this works with either Oracle's or MariaDB's client.

**C. With no local tooling**, from a runner. Upload the `.sql` as an asset on a
**private** GitHub Release tagged `db-seed`, then run
`.github/workflows/seed.yml` from *Actions → seed database → Run workflow*. It
accepts `.sql` or `.sql.gz`, restores, and prints the row counts afterwards.

### 2.3 A smaller dump, if 52 MB is awkward

`contacts`, `contact_emails` and `contact_phones` are a materialised view of
`records` (`DERIVED_TABLES` in `db.py`). Dump only the sources of truth —
`records`, `skipped`, `enrichment`, `sent_log`, `app_state`, `slack_users` —
then rebuild the merge on the far side:

```bash
npm run db:rebuild-contacts
```

It recomputes every contact from `records` in seconds, with no network and no
re-scraping.

### 2.4 Verify

```bash
python dbdump.py status
npm run db:phones:status
```

Compare against the same commands run locally; they should match exactly.
`db.bootstrap()` also migrates an older dump forward on first start, so a dump
from schema 7 is brought to 8 automatically.

---

## Step 3 — The API

### 3.1 The image

`Dockerfile` is in the repo root and needs no changes. It is `python:3.12-slim`
plus `default-mysql-client` (so `db:export` / `db:import` keep working), sets
`HOST=0.0.0.0`, `PORT=7860` and `MYSQL_SSL=1`, and runs `python -u server.py`.
`.dockerignore` keeps `db/data`, `backups/`, `.env` and the whole frontend out
of the image.

### 3.2 Deploy — a free container host

Pick by whether you mind the container going to sleep. Both build the
`Dockerfile` above straight from the private GitHub repo, so the source stays
private either way.

**Koyeb** — **Create Web Service** → GitHub → Dockerfile → **Free instance**,
health check `/api/db/status`. Historically Koyeb's free instance does not
idle-sleep, which makes it the better of the two.

**Render** — **New → Web Service** → connect the repo → **Runtime: Docker** →
**Instance type: Free** → **Health check path: `/api/db/status`**. Free
services spin down after 15 minutes idle and take 30–60 s to wake; see
[§12](#12-cold-starts-sleeping-and-keeping-it-awake).

Whichever you pick, choose the region closest to your database, then add
everything from [§3.4](#34-environment-variables) as environment variables.
The API is then at that host's URL.

### 3.3 Deploy — a second Vercel project instead

If the frontend is already on Vercel, the API can be too: one platform, no new
account, and the files for it are committed (`api/index.py`, `vercel.json`,
`public/`). It costs you background enrichment, `/api/db/export`, and anything
over 60 s. **[VERCEL.md](VERCEL.md) is the complete walkthrough** — it is the
path this project actually took.

> If a health check gets a 401, that is `API_TOKEN` doing its job. It still
> proves the process is alive, and most platforms treat any response as
> healthy; if yours insists on a 2xx, point the check at `/` or drop it.

Either way: pick the region closest to your database.

> If you set a health check path while `API_TOKEN` is set, the platform's
> probe will get a 401. That still proves the process is alive, and Render and
> Koyeb both treat any response as healthy — but if yours insists on a 2xx,
> point the check at `/` and expect 401, or drop the health check.

### 3.4 Environment variables

Set as secrets on the API host — never in the repo:

```
HOST=0.0.0.0
API_TOKEN=<generate one, see below>

MYSQL_HOST=mysql-xxxxxxx.aivencloud.com
MYSQL_PORT=12345
MYSQL_USER=avnadmin
MYSQL_PASSWORD=AVNS_...
MYSQL_DATABASE=email_scrapper
MYSQL_SSL=1

ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-4-8

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-char Gmail app password>
MAIL_FROM=you@gmail.com
MAIL_FROM_NAME=Ephrem

ENRICH=1
ENRICH_WORKERS=2
```

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`ENRICH_WORKERS=2` rather than 3: a free container has a fraction of a CPU, and
each worker is an outbound Claude call per contact. `ENRICH=0` turns it off
entirely if you would rather not spend credits from a service that may restart.

`GH_REPO` and `GH_DISPATCH_TOKEN` come later, in
[Step 6](#step-6--reconnect-the-rescrape-button).

### 3.5 Confirm it started

The startup banner tells you almost everything at once:

```
[db] avnadmin@<your-host>.aivencloud.com:<port>/email_scrapper ready
Loaded 12794 contacts (discourse=589, aboutme=410, github=11795)
Messaging: generate=on, send=on, from=you@gmail.com (2629 recipients messaged)
Enrichment: on (2059 cached, 2 workers)
Scrapes run in: this process
Auth: on (X-Api-Token)
Serving on http://0.0.0.0:7860  (Ctrl+C to stop)
```

`generate=OFF` or `send=OFF` means that secret did not reach the process — the
commonest deployment mistake, and the line that exists to catch it. `Auth: off`
while bound to `0.0.0.0` prints a loud warning; do not ignore it.

---

## Step 4 — The frontend

1. [vercel.com/new](https://vercel.com/new) → import `beeva/peoples`.
2. **Root Directory: `web`.** Vercel detects Next.js and uses `npm run build`.
3. **Environment Variables**, Production *and* Preview:

   | name | value |
   | --- | --- |
   | `API_BASE_URL` | your API host's URL |
   | `API_TOKEN` | the same token the API has |
   | `UI_USER` | a username you choose |
   | `UI_PASS` | a strong password |

4. Deploy.

None of these is `NEXT_PUBLIC_`, so all four stay on Vercel's servers — the
browser never learns the API's address or either secret.

`UI_USER` / `UI_PASS` drive `web/middleware.ts`, which puts HTTP Basic auth over
every route including `/api/*`. Leave them unset locally and there is no login;
set them on the deployment and there is one everywhere. This is not optional in
spirit: the directory holds names, addresses and phone numbers for thousands of
people who did not opt in to being in it.

### Cloudflare Pages instead

Commercial-use-safe and free:

```bash
npm --prefix web install --save-dev @opennextjs/cloudflare wrangler
```

**Workers & Pages → Create → Pages → Connect to Git**, root directory `web`,
build command `npx opennextjs-cloudflare build`. Same four variables. Cloudflare
Access can then gate it with an email one-time-code login on the free plan, in
which case you can leave `UI_USER`/`UI_PASS` unset.

### One caveat about `/api/db/export`

Streaming a ~52 MB dump through a serverless function exceeds Vercel Hobby's
limits, so the Database page's export button will fail there. Take backups from
`.github/workflows/backup.yml` instead — it runs nightly and keeps 30 days of
artifacts. Everything else in the UI is small JSON and fine.

---

## Step 5 — The scrapers, on GitHub Actions

Free (unlimited minutes on public repos, 2,000/month on private), 6 hours per
job. The passes are resumable — visited sets and cursors live in the database —
so successive runs continue rather than starting over.

Three workflows are already committed:

| file | what it does | trigger |
| --- | --- | --- |
| `.github/workflows/scrape.yml` | runs one scraper | 02:00 UTC daily, manual, or the Rescrape button |
| `.github/workflows/backup.yml` | dumps the database to a 30-day artifact | 03:00 UTC daily, manual |
| `.github/workflows/seed.yml` | restores a dump from a Release asset | manual |

### 5.1 Repository secrets

**Settings → Secrets and variables → Actions → New repository secret**, for
each of: `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`,
`MYSQL_DATABASE`, `ANTHROPIC_API_KEY`, `SCRAPE_GITHUB_TOKEN`.

The GitHub token must be called `SCRAPE_GITHUB_TOKEN` — `GITHUB_TOKEN` is
reserved by Actions and cannot be set as a secret. Without a real token, GitHub
search is 10 requests/minute and a run is pointless.

### 5.2 Run one

*Actions → scrape → Run workflow*, pick a source (`phones`, `github`,
`aboutme`, `devto`, `discourse`) and arguments (`--limit 5000`). The scheduled
run does the phone pass nightly.

Two things this gets right by how the project is already built:
`phone_pass.py` turns `SIGTERM` into the interrupt it handles, so hitting the
350-minute timeout writes the resume set and folds in what was found — a
timed-out run is a *partial* run, not a lost one. And because every scraper
writes into MySQL as it goes, results appear in the live UI while the workflow
is still running.

One caution: scrapes from GitHub's shared runner IPs are rate-limited harder
than from a residential address.

---

## Step 6 — Reconnect the Rescrape button

With two more variables on the API host, `POST /api/scrape` dispatches
`scrape.yml` instead of spawning a subprocess, and the UI works as before —
same button, same filter targeting, same live-updating list.

```
GH_REPO=beeva/peoples
GH_DISPATCH_TOKEN=github_pat_...
```

The token is a **fine-grained personal access token**, scoped to this repository
only, with **Actions: read and write**. Optional extras: `GH_WORKFLOW`
(default `scrape.yml`) and `GH_REF` (default `main`).

What happens then:

- **Start** — the button dispatches the workflow with the arguments the local
  scraper would have received, including the country / age / gender / date
  filters currently applied to the list.
- **Progress** — `/api/scrape/status` reads the run's state from GitHub, cached
  for 5 seconds so a 1.5-second UI poll does not spend the API rate limit.
  Queued and in-progress read as `running`; success as `done`; cancelled as
  `stopped`; failure and timeout as `error`.
- **Stop** — the Stop button cancels the run. In the few seconds before GitHub
  has created it, stopping says so rather than pretending.
- **The banner** confirms which mode you are in:
  `Scrapes run in: GitHub Actions (beeva/peoples)`.

Leave both unset and scrapes run in-process exactly as they always have — which
is what you want locally.

---

## 11. Alternative — everything on Vercel

Fewest accounts of any option: one platform, two projects, no separate API host.
Vercel's Python runtime serves a `BaseHTTPRequestHandler` subclass directly, and
`server.py`'s `Handler` is exactly that, so the glue is small.

> **[VERCEL.md](VERCEL.md) is the full walkthrough for this path**, with the
> exact variables and the verification steps. What follows is the summary.

Take this path for the simplest possible operation. Do **not** take it if
enrichment matters.

**What you give up:**

- **Background enrichment stops** — worker threads die with each invocation.
  Set `ENRICH=0`. (`_enqueue_rows` returns immediately when enrichment is off,
  so the read path stays clean.)
- **A 60-second ceiling** per request on Hobby. Fine for list and detail;
  `/api/db/export` and `/api/db/import` will not work.
- **A connection per cold start.** Aiven's free plan allows few concurrent
  connections and serverless opens one per cold container. Prefer TiDB Cloud
  here, which tolerates that pattern much better.
- **`db.bootstrap()` on every cold start**: ten `CREATE TABLE IF NOT EXISTS`
  statements plus a migration check, each a round-trip to a remote database.

Create `api/index.py`:

```python
"""Vercel Python entry point: serve server.py's Handler as a function."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import server      # noqa: E402

if os.environ.get("SKIP_BOOTSTRAP") != "1":
    db.bootstrap(verbose=False)
server.load_state()

handler = server.Handler
```

and `vercel.json`:

```json
{ "rewrites": [{ "source": "/api/(.*)", "destination": "/api/index" }] }
```

Deploy it as a **second Vercel project** with root directory `.` (the first
keeps root `web`). Both are free. Point the web project's `API_BASE_URL` at the
API project's URL.

Check on first deploy that the rewrite preserves the original path — `Handler`
routes on `self.path`. If every route 404s, log `self.path` once at the top of
`do_GET` and adjust the rewrite.

---

## 12. Cold starts, sleeping, and keeping it awake

Render's free tier sleeps after 15 minutes idle and takes 30–60 s to wake. Your
Next.js server component fetches the API during render; if that fetch outlives
the request the page shows *"Could not reach the data server at …"* rather than
data, and the first visit of the day looks broken.

In order of preference:

1. **Use a host that does not sleep** — Vercel functions or Koyeb. This
   removes the problem rather than papering over it, and costs nothing.
2. **Ping it externally** — [UptimeRobot](https://uptimerobot.com) (50 monitors
   free, 5-minute interval) or [cron-job.org](https://cron-job.org) against
   `https://your-api/api/db/status`. It will get a 401 once `API_TOKEN` is set,
   which still wakes the container. Render's free tier is 750 instance-hours a
   month against a ~730-hour month, so exactly one always-awake service fits.
3. **Not GitHub Actions.** Every 5 minutes is 8,640 runs a month against a
   2,000-minute allowance on a private repo. It will eat the budget you need for
   scraping.

---

## 13. Security checklist

- [ ] **`API_TOKEN` set on the API host and on Vercel.** Without it,
      `POST /api/db/import` is arbitrary SQL execution — read, write, drop —
      for anyone who finds the URL. Verified by the 401 test in
      [§15](#15-verification).
- [ ] **`UI_USER` / `UI_PASS` set** (or Cloudflare Access in front). The
      middleware covers `/api/*` as well as pages, so the data is not reachable
      around the login.
- [ ] **`GET /api/db/export` returns the entire database** — every scraped
      address and number. Same protection, same reason.
- [ ] **`POST /api/message/send` sends mail as you**, through the SMTP account
      in the environment. An unprotected endpoint is an open relay on your
      domain's reputation and your Gmail app password.
- [ ] **`.env` is never committed.** It is git-ignored; use each platform's
      secret store. `git log -p -- .env` should return nothing.
- [ ] **Rotate every secret that has been in a shell history, a screenshot or a
      chat** — Anthropic key, GitHub tokens, Gmail app password — before they
      live on three platforms instead of one.
- [ ] **Keep the repository private** if any dump, artifact or release will hold
      scraped data. Actions artifacts on a public repo are public.
- [ ] **Use the database provider's IP allowlist** where offered, and TLS
      always.
- [ ] **The data itself is regulated.** Scraped personal data falls under GDPR
      in the EU and similar regimes elsewhere: people have a right to know you
      hold it and to have it erased, and outbound mail must carry an opt-out.
      Publishing this to an open URL is a materially different act from keeping
      it on your laptop.

---

## 14. When free runs out

| limit | where you hit it | what to do |
| --- | --- | --- |
| **Aiven 5 GB** | ~35× today's data | TiDB Cloud (25 GB) or Aiven paid |
| **Free container 512 MB RAM** | large exports built in memory | export via `backup.yml` |
| **Actions 2,000 min/month** | ~6 h/day of scraping on a private repo | make the repo public (unlimited), or scrape less often |
| **Vercel 100 GB bandwidth** | not reachable by a private tool | — |
| **Anthropic credits** | enrichment across 12,000+ contacts | `ENRICH=0`, enrich in batches |
| **Gmail SMTP ~500/day** | a real outreach campaign | a transactional provider (Resend, Brevo) |

The first thing you would ever pay for is the database, and not soon.

---

## 15. Verification

```bash
API=https://your-api-host
TOK=your-api-token

# 1. Auth works -- this MUST be 401, not data.
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/db/status"

# 2. API up, MySQL reachable: server version and per-table row counts.
curl -s -H "X-Api-Token: $TOK" "$API/api/db/status" | head -40

# 3. The read path.
curl -s -H "X-Api-Token: $TOK" "$API/api/emails?source=all&per_page=5" | head -40

# 4. The phone work specifically.
curl -s -H "X-Api-Token: $TOK" \
     "$API/api/emails?source=all&contactable=whatsapp&per_page=3" | head -20
```

Then against the Vercel URL:

```bash
UI=https://your-app.vercel.app

# 5. The UI login is enforced.
curl -s -o /dev/null -w '%{http_code}\n' "$UI/"              # 401
curl -s -o /dev/null -w '%{http_code}\n' "$UI/api/db"        # 401
curl -s -o /dev/null -w '%{http_code}\n' -u user:pass "$UI/" # 200
```

And in the browser:

- [ ] The list loads and the total matches `python dbdump.py status`.
- [ ] Facets — country, gender, **Reach**, run — filter, and counts move.
- [ ] A contact opens and shows its detail pane, with phone numbers.
- [ ] CSV export downloads and opens, with the phone columns.
- [ ] Message generation returns text — proves `ANTHROPIC_API_KEY` arrived.
- [ ] Send a message **to yourself** — proves SMTP and the sent-log write.
- [ ] Redeploy the API and reload — proves the data is in MySQL, not in a
      container layer.
- [ ] Press **Rescrape** with `GH_REPO` set — a run appears at
      `github.com/<repo>/actions`, the button says "Scraping…", and new
      contacts stream into the list while it runs.

---

## 16. Troubleshooting

| symptom | cause | fix |
| --- | --- | --- |
| Health check fails, platform kills the container | still binding loopback | set `HOST=0.0.0.0` |
| Health check gets 401 | `API_TOKEN` is set, as intended | point the check at `/` and accept 401, or remove it |
| `(2003, "Can't connect to MySQL server")` | TLS not enabled | set `MYSQL_SSL=1` |
| `(1044, "Access denied … 'CREATE DATABASE'")` | host withholds the privilege | already tolerated — pre-create the database and set `MYSQL_DATABASE` |
| Import stops part-way, "packet too large" | server `max_allowed_packet` is 1 MB | raise it on the service ([§1.2](#12-raise-the-packet-limit)) |
| Restore fails on a `CREATE TABLE` | MariaDB dump into MySQL 8 | use the data-only recipe in [§2.1](#21-a-dialect-warning-because-it-will-bite-you-otherwise) |
| UI shows "Could not reach the data server" | API asleep, wrong `API_BASE_URL`, or token mismatch | [§12](#12-cold-starts-sleeping-and-keeping-it-awake); compare both `API_TOKEN` values |
| Every route 401s from the UI | `API_TOKEN` differs between the two | they must be byte-identical |
| `Messaging: generate=OFF` in the log | `ANTHROPIC_API_KEY` did not reach the process | re-add as a platform secret, redeploy |
| Pages slow, everything else fine | API and database in different regions | redeploy the API in the database's region |
| Rescrape returns "GitHub refused the dispatch (401)" | bad or unscoped `GH_DISPATCH_TOKEN` | fine-grained PAT, this repo, Actions: read and write |
| Rescrape returns "GitHub refused the dispatch (404)" | wrong `GH_REPO`, or `scrape.yml` not on `GH_REF` | push the workflow to the branch named by `GH_REF` |
| Stop says "the run has not started yet" | dispatched, run not created | wait a few seconds and press again |
| Contact list empty after a schema bump | migration dropped the derived tables | `npm run db:rebuild-contacts` |

---

## Summary — the shortest path

1. Aiven free MySQL; note the region; raise `max_allowed_packet`.
2. `npm run db:export`, then import into Aiven.
3. Deploy the API — a second Vercel project, or Koyeb; set the secrets from
   [§3.4](#34-environment-variables).
4. Vercel, root directory `web`, four environment variables.
5. Add the repository secrets; run `scrape.yml` once by hand.
6. Set `GH_REPO` + `GH_DISPATCH_TOKEN` on the API to get the Rescrape button back.
7. Work through [§15](#15-verification).

Total cost: nothing. Total machines: none.
