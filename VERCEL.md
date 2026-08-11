# Deploying on Vercel

Everything on one platform: **two Vercel projects from this one repository** —
the Next.js app and the Python API — talking to **Aiven MySQL**, with scrapes
on **GitHub Actions**. No other accounts, nothing to administer, no cost.

Secrets are never written into this file. Each table names the variable and
says where its value already lives.

---

## Contents

1. [The shape of it](#1-the-shape-of-it)
2. [What you are accepting](#2-what-you-are-accepting)
3. [Status: what is already done](#3-status-what-is-already-done)
4. [Step 1 — Deploy the API](#step-1--deploy-the-api)
5. [Step 2 — Deploy the frontend](#step-2--deploy-the-frontend)
6. [Step 3 — GitHub Actions secrets](#step-3--github-actions-secrets)
7. [Step 4 — Fix the dispatch token](#step-4--fix-the-dispatch-token)
8. [Step 5 — Verify](#step-5--verify)
9. [Loading the database](#loading-the-database)
10. [Every variable, every place](#every-variable-every-place)
11. [If you later want enrichment back](#if-you-later-want-enrichment-back)
12. [Troubleshooting](#troubleshooting)

---

## 1. The shape of it

```
                    ┌──────────────────────────────┐
   your browser ───▶│  Vercel project #1  — web/   │  Next.js, Basic auth
                    └───────────────┬──────────────┘
                                    │ server-side fetch, X-Api-Token
                                    ▼
                    ┌──────────────────────────────┐
                    │  Vercel project #2  — api/   │  server.py as a function
                    └───────────────┬──────────────┘
                                    │ MySQL over TLS
                                    ▼
                    ┌──────────────────────────────┐
                    │  Aiven MySQL 8.4  (free)     │  the archive
                    └───────────────▲──────────────┘
                                    │
                    ┌───────────────┴──────────────┐
                    │  GitHub Actions — scrapers   │  free, 6 h per job
                    └──────────────────────────────┘
```

Two projects, one repository, distinguished only by **Root Directory**: `web`
builds the frontend, `.` builds the API. Three files make the second one work
and are already committed:

| file | what it does |
| --- | --- |
| `api/index.py` | serves `server.py`'s `Handler` — Vercel's Python runtime takes a `BaseHTTPRequestHandler` subclass directly, so this is an adapter, not a second implementation |
| `vercel.json` | no framework, static output from `public/`, and `/api/*` rewritten to the function |
| `public/index.html` | the static output — deliberately almost empty |

> **Why `public/` exists.** Vercel's output directory is served as static
> files. Pointing it at the repository root would publish `server.py`, `db.py`,
> `.env.example` and these guides as fetchable assets on the API's own domain.
> `public/` holds one page and nothing else.

The browser never talks to the API. Every call goes through the Next.js server
— server components and the route handlers in `web/app/api/*` — so there is no
CORS to configure and the token never reaches the client.

---

## 2. What you are accepting

A serverless function ends when its request ends. Three consequences, all
known up front:

**Background enrichment stops.** `server.py` starts worker threads that infer
country and gender via Claude; they cannot outlive an invocation. Set
`ENRICH=0` — with it off, `_enqueue_rows` returns immediately, so the read path
stays clean rather than filling a queue nothing drains. Contacts already
enriched keep their country and gender; new ones simply do not get any.
[§11](#if-you-later-want-enrichment-back) is how to run it in batches anyway.

**`/api/db/export` will not work** through the UI — a ~53 MB response exceeds
Hobby's limits. `.github/workflows/backup.yml` takes a nightly dump instead and
keeps 30 days of it, which is the better arrangement regardless.

**60 seconds per request** on Hobby. This does not affect scrapes: they run on
GitHub Actions and the Rescrape button only dispatches them.

Everything else is unchanged — the list, all filtering and faceting, contact
detail, CSV export, phone/WhatsApp, message generation, and SMTP sending.

---

## 3. Status: what is already done

| | |
| --- | --- |
| Aiven MySQL 8.4.8 | ✅ running, TLS verified, `max_allowed_packet` 64 MB |
| The archive | ✅ **imported** — 58,717 rows, verified against local row-for-row |
| `ANTHROPIC_API_KEY` | ✅ `claude-opus-5` responds |
| `GITHUB_TOKEN` | ✅ 5,000/hr core, 30/min search |
| Gmail SMTP | ✅ app password accepted |
| `API_TOKEN` | ✅ generated |
| `GH_DISPATCH_TOKEN` | ❌ cannot see this repo — [Step 4](#step-4--fix-the-dispatch-token) |
| `UI_USER` / `UI_PASS` | — you choose them at [Step 2](#step-2--deploy-the-frontend) |

Only the dispatch token is outstanding, and it affects one button.

---

## Step 1 — Deploy the API

1. [vercel.com/new](https://vercel.com/new) → **Import** `beeva/email-scrapper`.
   (Approve Vercel's access to the private repo when GitHub asks.)
2. **Root Directory: `.`** — the repository root. Leave the framework preset
   as-is; `vercel.json` sets it to none.
3. **Environment Variables** — Production, Preview and Development:

| Variable | Value |
| --- | --- |
| `MYSQL_HOST` | `.env` — the `…aivencloud.com` host |
| `MYSQL_PORT` | `.env` — `19113` |
| `MYSQL_USER` | `.env` — `avnadmin` |
| `MYSQL_PASSWORD` | `.env` — the `AVNS_…` string |
| `MYSQL_DATABASE` | `email_scrapper` |
| `MYSQL_SSL` | `1` — Aiven refuses a plaintext connection |
| `API_TOKEN` | `.env` — the 43-character token |
| `ANTHROPIC_API_KEY` | `.env` — the `sk-ant-api03-…` key |
| `ANTHROPIC_MODEL` | `claude-opus-5` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `.env` — your Gmail address |
| `SMTP_PASSWORD` | `.env` — the 16-character app password |
| `MAIL_FROM` | same as `SMTP_USER` |
| `MAIL_FROM_NAME` | `Ephrem` |
| `GH_REPO` | `beeva/email-scrapper` |
| `GH_DISPATCH_TOKEN` | `.env` — the `github_pat_…` token |
| `ENRICH` | **`0`** — see [§2](#2-what-you-are-accepting) |

Do **not** set `HOST` or `PORT`; a function is not a socket the app binds.

4. **Deploy.**
5. **After the first successful deploy**, add one more variable and redeploy:

| Variable | Value |
| --- | --- |
| `SKIP_BOOTSTRAP` | `1` |

Without it, every cold start re-runs ten `CREATE TABLE IF NOT EXISTS`
statements against Aiven — affordable once, wasteful forever. Leave it unset
for the first deploy so the schema check runs, then set it.

### Confirm it

```bash
API=https://<your-api-project>.vercel.app

# The API is up and the token is enforced -- this MUST be 401.
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/db/status"

# With the token, real data.
curl -s -H "X-Api-Token: <API_TOKEN>" "$API/api/db/status" | head -20
```

The second should report MySQL `8.4.8` and per-table counts led by
`records: 13414`.

---

## Step 2 — Deploy the frontend

1. [vercel.com/new](https://vercel.com/new) → import **the same repository
   again** as a second project.
2. **Root Directory: `web`.** Vercel detects Next.js; everything else is
   correct by default.
3. **Environment Variables** — Production, Preview and Development:

| Variable | Value |
| --- | --- |
| `API_BASE_URL` | the Step 1 project's URL, e.g. `https://email-scrapper-api.vercel.app` |
| `API_TOKEN` | **byte-identical** to the API project's |
| `UI_USER` | a username you choose |
| `UI_PASS` | a strong password you choose |

4. **Deploy.**

Four things worth knowing:

- **None of these is `NEXT_PUBLIC_`, and none should be.** Every call to the
  API is made by the Next.js server. A `NEXT_PUBLIC_` name would inline the
  token into the browser bundle.
- **`API_TOKEN` mismatch is the commonest failure** and shows up as every route
  returning 401. Compare the two character by character rather than
  regenerating.
- **`UI_USER` / `UI_PASS` are new values you invent here** — they are not in
  `.env` and do not need to be. `python -c "import secrets; print(secrets.token_urlsafe(18))"`
  makes a good password. They drive `web/middleware.ts`, which covers `/api/*`
  as well as pages, so the data is not reachable around the login.
- **Leave all four unset locally.** Without `UI_USER`/`UI_PASS` the middleware
  passes everything through, which is what you want on localhost.

> Vercel Hobby is licensed for non-commercial use. If this directory feeds paid
> outreach, Cloudflare Pages and Netlify both run Next.js 15 free with no such
> clause — same four variables, same root directory.

---

## Step 3 — GitHub Actions secrets

For the nightly scrape, the nightly backup, and the work the Rescrape button
dispatches. **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret | Value |
| --- | --- |
| `MYSQL_HOST` | the Aiven host |
| `MYSQL_PORT` | `19113` |
| `MYSQL_USER` | `avnadmin` |
| `MYSQL_PASSWORD` | the `AVNS_…` string |
| `MYSQL_DATABASE` | `email_scrapper` |
| `ANTHROPIC_API_KEY` | the `sk-ant-api03-…` key |
| `SCRAPE_GITHUB_TOKEN` | the `ghp_…` scraping token from `.env` |

**The last name is not a typo.** `GITHUB_TOKEN` is reserved by Actions and
cannot be created as a secret; `scrape.yml` maps `SCRAPE_GITHUB_TOKEN` back to
`GITHUB_TOKEN` inside the job. `MYSQL_SSL: "1"` is already in the workflows.

---

## Step 4 — Fix the dispatch token

`GH_DISPATCH_TOKEN` authenticates correctly as `beeva`, but asking GitHub for
`beeva/email-scrapper` with it returns **404**, and listing what it *can* reach
returns only `beeva/good-web-game` and `beeva/web-ruggrogue`. A fine-grained
PAT returns 404 rather than 403 for a repository outside its selection, so this
is scope, not a bad token — the likely cause is that the token was created with
**Only select repositories** before `email-scrapper` existed, and fine-grained
PATs do not pick up repositories created after them.

1. [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens)
   → open the token.
2. **Repository access → Only select repositories → add `email-scrapper`.**
3. Confirm **Repository permissions → Actions → Read and write**.
4. **Update token** — the string does not change, so nothing else needs editing.

Re-check:

```bash
python -c "
import sys,os,json,urllib.request; sys.path.insert(0,'.'); import server
r=urllib.request.Request('https://api.github.com/repos/'+os.environ['GH_REPO'],
  headers={'Authorization':'Bearer '+os.environ['GH_DISPATCH_TOKEN'],
           'Accept':'application/vnd.github+json','User-Agent':'x'})
print(urllib.request.urlopen(r,timeout=30).status,'OK')"
```

`200 OK` means done. Until then Rescrape reports
*GitHub refused the dispatch (404)*; nothing else is affected.

---

## Step 5 — Verify

```bash
API=https://<your-api-project>.vercel.app
UI=https://<your-web-project>.vercel.app
TOK=<the API_TOKEN from .env>

curl -s -o /dev/null -w 'api, no token  → %{http_code}\n' "$API/api/db/status"
curl -s -H "X-Api-Token: $TOK" "$API/api/emails?source=all&per_page=3" | head -20
curl -s -H "X-Api-Token: $TOK" \
     "$API/api/emails?source=all&contactable=whatsapp&per_page=1" | head -5
curl -s -o /dev/null -w 'ui,  no auth   → %{http_code}\n' "$UI/"
curl -s -o /dev/null -w 'ui,  with auth → %{http_code}\n' -u user:pass "$UI/"
```

Expect `401`, a list totalling **12,794**, **99** WhatsApp contacts, then `401`
and `200`. In the browser:

- [ ] The list loads and the total matches `python dbdump.py status`.
- [ ] Country / gender / **Reach** / run facets filter, and counts move.
- [ ] A contact opens; phone numbers show where present.
- [ ] CSV export downloads with the phone columns.
- [ ] Message generation returns text — proves `ANTHROPIC_API_KEY` arrived.
- [ ] Send a message **to yourself** — proves SMTP and the sent-log write.
- [ ] **Rescrape** with a small limit → a run appears at
      `github.com/beeva/email-scrapper/actions`.

---

## Loading the database

**Already done** — 58,717 rows are in Aiven and verified. This section is for
re-running it later.

**The obvious route does not work on this machine.** XAMPP ships **MariaDB
10.4** — server *and* client — while Aiven serves **MySQL 8.4.8**, which
authenticates with `caching_sha2_password`, a plugin MariaDB's client does not
carry:

```
ERROR 1045 (28000): Plugin caching_sha2_password could not be loaded
```

That rules out `mysqldump | mysql` and `npm run db:import`, since both drive
those binaries. (`--ssl-mode=REQUIRED` also fails there — MariaDB spells it
`--ssl`, which `dbdump.py` handles automatically; it cannot conjure a missing
auth plugin.)

Use PyMySQL, which speaks that auth natively and is already the only
dependency. Moving rows as Python objects also sidesteps SQL dialect, quoting
and reserved words entirely:

```bash
# .env points MYSQL_* at Aiven, so the local launcher would see Aiven answering
# and decide a server is "already running". Override it for the local one.
MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_USER=root MYSQL_PASSWORD= MYSQL_SSL= \
  node scripts/mysql-server.js &

npm run db:copy -- --truncate      # scripts/copy_to_aiven.py
python dbdump.py status            # verify
```

`--truncate` empties the destination first, so the copy is re-runnable. If
`contacts` is ever short but `records` is right, `npm run db:rebuild-contacts`
recomputes the merge from `records` without touching the network.

> To use the CLI instead, install Oracle's MySQL client and point
> `MYSQL_BASEDIR` at it. `dbdump.py` probes the binary for `--ssl-mode` support
> and adapts, so `npm run db:export` / `db:import` then work against Aiven too.

---

## Every variable, every place

| Variable | Local `.env` | Vercel — API | Vercel — web | Actions secrets |
| --- | :---: | :---: | :---: | :---: |
| `MYSQL_HOST` / `PORT` / `USER` / `PASSWORD` / `DATABASE` | ✅ | ✅ | — | ✅ |
| `MYSQL_SSL` | ✅ | ✅ | — | in workflow |
| `API_TOKEN` | ✅ | ✅ | ✅ **identical** | — |
| `API_BASE_URL` | — | — | ✅ | — |
| `UI_USER` / `UI_PASS` | — | — | ✅ | — |
| `ANTHROPIC_API_KEY` | ✅ | ✅ | — | ✅ |
| `ANTHROPIC_MODEL` | ✅ | ✅ | — | — |
| `SMTP_*` / `MAIL_FROM*` | ✅ | ✅ | — | — |
| `ENRICH` | `1` | **`0`** | — | `0` in workflow |
| `SKIP_BOOTSTRAP` | — | `1` after first deploy | — | — |
| `GITHUB_TOKEN` | ✅ | — | — | as `SCRAPE_GITHUB_TOKEN` |
| `GH_REPO` / `GH_DISPATCH_TOKEN` | ✅ | ✅ | — | — |
| `HOST` / `PORT` | blank | **never** | — | — |

Two entries in `.env` are read by nothing:

- **`AIVEN_TOKEN`** — an Aiven *account API* token. No code uses it and it can
  administer your whole Aiven account. Delete the line; revoke it in the Aiven
  console unless another tool needs it.
- **`HUGGING_SPACE_TOKEN`** — left over from an abandoned host. Delete it and
  revoke it at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).

---

## If you later want enrichment back

Two options, neither urgent.

**Run it locally in batches.** Point `.env` at Aiven (it already is), start the
server on your own machine with `ENRICH=1`, and browse the list — the workers
fill in country and gender for the contacts on each page, writing straight to
Aiven. Stop it when you have had enough. This costs nothing beyond Claude
credits and needs no hosting at all.

**Move the API to a container host.** A long-lived process runs the workers as
designed. **Koyeb**'s free instance does not idle-sleep and builds the same
`Dockerfile` from this private repo; **Render**'s free tier works but sleeps
after 15 minutes, so the first page load of the day waits ~50 s unless an
external pinger keeps it warm. Both take the same variables as Step 1 with
`ENRICH=1`. The `Dockerfile` in this repo is ready for either.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every API route 404s, body shows `received_path` | the rewrite delivered the destination path | compare `received_path` against `vercel.json`'s `rewrites` and adjust the rule |
| Function fails at import, `unsupported operand type(s) for \|` | Vercel gave it Python < 3.10; `db.py` uses `str \| None` | pin Python 3.12 in the project's settings |
| Every route 401s from the UI | the two `API_TOKEN` values differ | compare character by character |
| UI: "Could not reach the data server" | wrong `API_BASE_URL`, or the API project failed to build | open `$API/api/db/status` directly |
| `(2003, "Can't connect to MySQL")` | `MYSQL_SSL=1` missing on the API project | Aiven refuses plaintext |
| `Messaging: generate=OFF` in logs | `ANTHROPIC_API_KEY` missing | re-add and redeploy |
| Rescrape → "GitHub refused the dispatch (404)" | token scope, or `scrape.yml` not on `main` | [Step 4](#step-4--fix-the-dispatch-token) |
| Rescrape → "(401)" | bad `GH_DISPATCH_TOKEN` | regenerate the PAT |
| Cold starts feel slow / Aiven connection errors | a new DB connection per cold container | set `SKIP_BOOTSTRAP=1`; Aiven's free plan caps concurrent connections |
| `server.py` or `.env.example` fetchable on the API domain | `outputDirectory` is not `public` | check `vercel.json` |
| List empty after a schema change | migration dropped the derived tables | `npm run db:rebuild-contacts` |
| `npm run db:start` says "already listening" | `.env` points `MYSQL_*` at Aiven | override on the command line — see [Loading the database](#loading-the-database) |
