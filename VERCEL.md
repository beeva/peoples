# Deploying to Vercel

A focused, step-by-step version of [DEPLOY.md](DEPLOY.md) for the stack you
have already provisioned: **Aiven MySQL** (live), **Vercel** for the app, and
**GitHub Actions** for scrapes.

Secrets are never written into this file — every table says *which* variable to
paste and where its value lives in your local `.env`.

---

## Contents

1. [The shape of it](#1-the-shape-of-it)
2. [Two things to fix first](#2-two-things-to-fix-first)
3. [Step 1 — Load your data into Aiven](#step-1--load-your-data-into-aiven)
4. [Step 2 — Deploy the API](#step-2--deploy-the-api)
5. [Step 3 — Deploy the frontend to Vercel](#step-3--deploy-the-frontend-to-vercel)
6. [Step 4 — GitHub Actions secrets](#step-4--github-actions-secrets)
7. [Step 5 — Verify](#step-5--verify)
8. [Appendix A — Everything on Vercel](#appendix-a--everything-on-vercel)
9. [Appendix B — Every variable, every place](#appendix-b--every-variable-every-place)
10. [Troubleshooting](#troubleshooting)

---

## 1. The shape of it

Vercel runs the Next.js app. It cannot run `server.py` as a *long-lived*
process, and that matters for one specific feature: the background enrichment
workers (`server.py` starts threads that infer country and gender via Claude)
only exist while a process stays alive. A serverless function ends when the
request ends, and the threads die with it.

So there are two layouts, and the difference between them is one feature:

| | API on a free container **(recommended)** | API on Vercel functions |
| --- | --- | --- |
| Frontend | Vercel | Vercel |
| API | Hugging Face Space / Koyeb / Render | Vercel (2nd project) |
| Background enrichment | **works** | stops — set `ENRICH=0` |
| `/api/db/export` in the UI | works | fails (payload too large) |
| Rescrape button | works (GitHub Actions) | works (GitHub Actions) |
| Request time limit | none | 60 s |
| Platforms to manage | 2 | 1 |

You have `ENRICH=1` and 2,059 contacts already enriched, so you are using that
feature — which is why the container layout is the recommendation. Steps 1–5
below cover it. If you would rather keep everything on one platform,
[Appendix A](#appendix-a--everything-on-vercel) is the complete alternative and
the files it needs are already in the repo.

---

## 2. Two things to fix first

Both were found by testing your actual credentials. Neither blocks you for
long, but the deployment is not useful until they are done.

### 2.1 Your Aiven database is empty

The schema exists now (all 10 tables), but there are **0 records**. Your 12,794
contacts are still only on this machine. [Step 1](#step-1--load-your-data-into-aiven)
moves them.

### 2.2 `GH_DISPATCH_TOKEN` cannot see this repository

The token is valid and belongs to `beeva`, but asking GitHub for
`beeva/email-scrapper` with it returns **404**, and listing what it *can* reach
returns only `beeva/good-web-game` and `beeva/web-ruggrogue`. A fine-grained PAT
returns 404 rather than 403 for a repository outside its selection, so this is a
scope problem, not a bad token.

The likely cause: the token was created with **Only select repositories**, and
`email-scrapper` did not exist yet when you picked the list — it was pushed
during this session, and fine-grained PATs do not pick up repositories created
after them.

**Fix:**

1. [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens)
   → click the token.
2. **Repository access** → **Only select repositories** → add **`email-scrapper`**.
3. Confirm **Repository permissions → Actions → Read and write** is set.
4. **Update token** — the token string does not change, so `.env` stays as-is.

**Re-check:**

```bash
python -c "
import sys,os,json,urllib.request; sys.path.insert(0,'.'); import server
r=urllib.request.Request('https://api.github.com/repos/'+os.environ['GH_REPO'],
  headers={'Authorization':'Bearer '+os.environ['GH_DISPATCH_TOKEN'],
           'Accept':'application/vnd.github+json','User-Agent':'x'})
print(urllib.request.urlopen(r,timeout=30).status, 'OK')"
```

`200 OK` means done. Until it passes, the Rescrape button will report
*GitHub refused the dispatch (404)* — everything else works regardless.

---

## Step 1 — Load your data into Aiven

**The obvious route does not work here, and it is worth knowing why before you
try it.** XAMPP ships **MariaDB 10.4** — both the server *and* the client
binaries — while Aiven serves **MySQL 8.4.8**. MySQL 8 authenticates with
`caching_sha2_password`, a plugin MariaDB's client does not have, so
`mysql.exe` against Aiven dies at the handshake:

```
ERROR 1045 (28000): Plugin caching_sha2_password could not be loaded:
The specified module could not be found. Library path is 'caching_sha2_password.dll'
```

That rules out `mysqldump | mysql` and `python dbdump.py import` for this
machine — both drive those binaries. (`--ssl-mode=REQUIRED` also fails on the
MariaDB client, which spells it `--ssl`; `dbdump.py` handles that difference
automatically, but it cannot fix the missing auth plugin.)

**Use PyMySQL instead.** It speaks `caching_sha2_password` natively, is already
the project's only dependency, and moving rows as Python objects sidesteps SQL
dialect and quoting entirely. `scripts/copy_to_aiven.py` does exactly this:
streams each table out of the local server and batch-inserts it into Aiven,
matching columns by name.

```bash
# 1. Start the local database. Note the overrides: .env now points MYSQL_* at
#    Aiven, so without them scripts/mysql-server.js sees Aiven answering and
#    decides a server is "already running" instead of starting the local one.
MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_USER=root MYSQL_PASSWORD= MYSQL_SSL= \
  node scripts/mysql-server.js &

# 2. Copy every table across.
python scripts/copy_to_aiven.py
```

**Verify** — `.env` already points at Aiven:

```bash
python dbdump.py status
```

Counts should match the local ones: 13,414 records, 12,827 contacts, 17,307
emails, 694 phones. If `contacts` is short but `records` is right, run
`npm run db:rebuild-contacts` — the merge recomputes from `records` in seconds
without touching the network.

> **If you would rather use the CLI**, install Oracle's MySQL client (MySQL
> Shell, or the `mysql` from a MySQL 8 install) and put its directory in
> `MYSQL_BASEDIR`. `dbdump.py` probes the binary for `--ssl-mode` support and
> adapts, so `npm run db:export` / `db:import` then work against Aiven too.

---

## Step 2 — Deploy the API

The repo has a `Dockerfile` that needs no changes. Hugging Face Spaces is the
pick: 2 vCPU, 16 GB RAM, free, private, and — unlike Render's free tier — it
does not sleep after 15 minutes.

1. [huggingface.co/new-space](https://huggingface.co/new-space) → **SDK: Docker**
   → **Blank** → **Private**.
2. Add this to the top of the Space's `README.md`:

   ```yaml
   ---
   title: email-scrapper API
   sdk: docker
   app_port: 7860
   ---
   ```

3. Push this repo to the Space:

   ```bash
   git remote add space https://huggingface.co/spaces/<you>/email-scrapper-api
   git push space main
   ```

4. **Settings → Variables and secrets** → add each of these as a **Secret**:

| Variable | Value comes from |
| --- | --- |
| `MYSQL_HOST` | `.env` — the `…aivencloud.com` host |
| `MYSQL_PORT` | `.env` — `19113` |
| `MYSQL_USER` | `.env` — `avnadmin` |
| `MYSQL_PASSWORD` | `.env` — the `AVNS_…` string |
| `MYSQL_DATABASE` | `email_scrapper` |
| `MYSQL_SSL` | `1` |
| `API_TOKEN` | `.env` — the 43-character token |
| `ANTHROPIC_API_KEY` | `.env` — the `sk-ant-api03-…` key |
| `ANTHROPIC_MODEL` | `claude-opus-5` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `.env` — your Gmail address |
| `SMTP_PASSWORD` | `.env` — the 16-character app password |
| `MAIL_FROM` | same as `SMTP_USER` |
| `MAIL_FROM_NAME` | `Ephrem` |
| `ENRICH` | `1` |
| `ENRICH_WORKERS` | `2` — a free container has a fraction of a CPU |
| `GH_REPO` | `beeva/email-scrapper` |
| `GH_DISPATCH_TOKEN` | `.env` — the `github_pat_…` token (after fixing §2.2) |

`HOST` and `PORT` are already set by the `Dockerfile` — do not add them.

**The startup log is the test.** Look for:

```
[db] avnadmin@mysql-23deed53-….aivencloud.com:19113/email_scrapper ready
Loaded 12794 contacts (discourse=589, aboutme=410, github=11795)
Messaging: generate=on, send=on, from=asefaephrem000@gmail.com
Enrichment: on (2059 cached, 2 workers)
Scrapes run in: GitHub Actions (beeva/email-scrapper)
Auth: on (X-Api-Token)
Serving on http://0.0.0.0:7860
```

Your API URL is then `https://<you>-email-scrapper-api.hf.space`.

---

## Step 3 — Deploy the frontend to Vercel

1. [vercel.com/new](https://vercel.com/new) → **Import** `beeva/email-scrapper`.
   (Vercel needs access to a private repo — approve it when GitHub asks.)
2. **Root Directory → `web`.** This is the one setting that matters; Vercel
   then auto-detects Next.js and everything else is correct by default.
3. **Environment Variables** — add all four, ticked for **Production**,
   **Preview** and **Development**:

| Variable | Value | Why |
| --- | --- | --- |
| `API_BASE_URL` | `https://<you>-email-scrapper-api.hf.space` | where the Next.js server fetches data from |
| `API_TOKEN` | **exactly** the value from `.env` | must be byte-identical to the API's, or every request 401s |
| `UI_USER` | a username you choose | Basic-auth login (`web/middleware.ts`) |
| `UI_PASS` | a strong password you choose | ditto |

4. **Deploy.**

Four things worth knowing about this list:

- **None of them is `NEXT_PUBLIC_`, and none should be.** The browser never
  talks to the Python API — every call goes through the Next.js server. A
  `NEXT_PUBLIC_` name would inline the token into the client bundle.
- **`API_BASE_URL` has no trailing slash requirement** — `web/lib/emails.ts`
  strips it either way.
- **`UI_USER`/`UI_PASS` are new values you invent here.** They are not in your
  `.env` and do not need to be. Generate a password with
  `python -c "import secrets; print(secrets.token_urlsafe(18))"`.
- **Leave them all unset locally.** Without `UI_USER`/`UI_PASS` the middleware
  passes everything through, which is what you want on localhost.

> **Vercel Hobby is licensed for non-commercial use.** If this directory feeds
> paid outreach, use Cloudflare Pages or Netlify instead — same four variables,
> same root directory.

---

## Step 4 — GitHub Actions secrets

For the nightly scrape, the backup workflow, and the Rescrape button's actual
work. **Settings → Secrets and variables → Actions → New repository secret**:

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
cannot be created as a secret; `.github/workflows/scrape.yml` maps
`SCRAPE_GITHUB_TOKEN` back to `GITHUB_TOKEN` inside the job.

`MYSQL_SSL: "1"` is already hard-coded in the workflows.

---

## Step 5 — Verify

```bash
API=https://<you>-email-scrapper-api.hf.space
UI=https://<your-project>.vercel.app
TOK=<the API_TOKEN from .env>

# 1. Auth is on -- this MUST be 401.
curl -s -o /dev/null -w 'api, no token  → %{http_code}\n' "$API/api/db/status"

# 2. With the token, real data.
curl -s -H "X-Api-Token: $TOK" "$API/api/db/status" | head -20

# 3. The UI login is on -- 401, then 200.
curl -s -o /dev/null -w 'ui,  no auth   → %{http_code}\n' "$UI/"
curl -s -o /dev/null -w 'ui,  with auth → %{http_code}\n' -u user:pass "$UI/"
```

Expect `401`, real JSON, `401`, `200`. Then in the browser:

- [ ] The list loads and the total matches `python dbdump.py status`.
- [ ] Country / gender / **Reach** / run facets filter, and counts move.
- [ ] A contact opens; phone numbers show where present.
- [ ] CSV export downloads with the phone columns.
- [ ] Message generation returns text (proves `ANTHROPIC_API_KEY` arrived).
- [ ] Send a message **to yourself** (proves SMTP + the sent-log write).
- [ ] **Rescrape** with a small limit → a run appears at
      `github.com/beeva/email-scrapper/actions` and the button says "Scraping…".

---

## Appendix A — Everything on Vercel

One platform, two Vercel projects from the same repo. `api/index.py` and
`vercel.json` are already committed for this.

**What you give up:** background enrichment (set `ENRICH=0`), `/api/db/export`
through the UI, and anything that takes over 60 s. Scrapes still work — they run
on GitHub Actions either way. Enrichment can still be done in batches by
running `python server.py` on your own machine against Aiven whenever you like.

1. **vercel.com/new** → import the same repo again → **Root Directory: `.`**
   (the repository root, *not* `web`).
2. Vercel reads the committed `vercel.json`: no build step, and every `/api/*`
   path rewritten to the Python function in `api/index.py`, which serves
   `server.py`'s `Handler` directly.
3. Environment variables — the same list as
   [Step 2](#step-2--deploy-the-api), with two changes:

   | Variable | Value |
   | --- | --- |
   | `ENRICH` | `0` — worker threads cannot survive a serverless invocation |
   | `SKIP_BOOTSTRAP` | `1` — **add this after the first successful deploy** |

   `SKIP_BOOTSTRAP=1` stops the adapter re-running ten `CREATE TABLE IF NOT
   EXISTS` statements against Aiven on every cold start. Leave it unset for the
   first deploy so the schema is created, then set it.

4. Point the web project's `API_BASE_URL` at this project's URL.

**Check on the first deploy** that the rewrite preserves the request path —
`Handler` routes on `self.path`. If every route 404s, log `self.path` once at
the top of `do_GET` and adjust the `rewrites` rule in `vercel.json` to match
what actually arrives. This is the one part of the all-Vercel path that cannot
be verified from here, which is the honest reason the container layout is the
recommendation.

**Aiven's free plan allows a limited number of concurrent connections**, and a
serverless function opens a new one per cold container. Fine for a private tool
with a couple of users; if you see connection errors under load, that is the
cause, and TiDB Cloud's free tier tolerates the pattern much better.

---

## Appendix B — Every variable, every place

| Variable | Local `.env` | API host | Vercel (web) | Actions secrets |
| --- | :---: | :---: | :---: | :---: |
| `MYSQL_HOST` / `PORT` / `USER` / `PASSWORD` / `DATABASE` | ✅ | ✅ | — | ✅ |
| `MYSQL_SSL` | ✅ | Dockerfile | — | in workflow |
| `API_TOKEN` | ✅ | ✅ | ✅ **identical** | — |
| `API_BASE_URL` | — | — | ✅ | — |
| `UI_USER` / `UI_PASS` | — | — | ✅ | — |
| `HOST` / `PORT` | blank | Dockerfile | — | — |
| `ANTHROPIC_API_KEY` | ✅ | ✅ | — | ✅ |
| `ANTHROPIC_MODEL` | ✅ | ✅ | — | — |
| `SMTP_*` / `MAIL_FROM*` | ✅ | ✅ | — | — |
| `ENRICH` / `ENRICH_WORKERS` | ✅ | ✅ | — | `0` in workflow |
| `GITHUB_TOKEN` | ✅ | — | — | as `SCRAPE_GITHUB_TOKEN` |
| `GH_REPO` / `GH_DISPATCH_TOKEN` | ✅ | ✅ | — | — |
| `GH_WORKFLOW` / `GH_REF` | defaults | — | — | — |

Two entries in your `.env` are not used by anything:

- **`AIVEN_TOKEN`** — an Aiven *account API* token. No code reads it, and it can
  administer your whole Aiven account. Delete it from `.env`, and revoke it in
  the Aiven console unless another tool needs it.
- **`API_BASE_URL=`** (blank, line 4) — a frontend variable. `server.py` ignores
  it; the real one goes on Vercel.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| UI: "Could not reach the data server" | wrong `API_BASE_URL`, or the API is down | open `$API/api/db/status` directly |
| Every route 401s from the UI | the two `API_TOKEN` values differ | compare character by character; don't regenerate |
| UI loads but the list is empty | data never imported | [Step 1](#step-1--load-your-data-into-aiven) |
| `Messaging: generate=OFF` | `ANTHROPIC_API_KEY` missing on the API host | re-add as a secret, redeploy |
| `Scrapes run in: this process` | `GH_REPO`/`GH_DISPATCH_TOKEN` missing on the API host | add both |
| Rescrape → "GitHub refused the dispatch (404)" | token can't see the repo, or `scrape.yml` isn't on `main` | [§2.2](#22-gh_dispatch_token-cannot-see-this-repository), and push the workflows |
| Rescrape → "(401)" | bad `GH_DISPATCH_TOKEN` | regenerate the PAT |
| `(2003, "Can't connect to MySQL")` | `MYSQL_SSL=1` missing | Aiven refuses plaintext |
| Restore fails on a `CREATE TABLE` | MariaDB dump into MySQL 8.4 | use the data-only recipe in [Step 1](#step-1--load-your-data-into-aiven) |
| `1064 … syntax error near 'manual'` | pre-fix code against MySQL 8.4 | already fixed — `manual` is backticked; pull latest |
| Vercel build fails at the root | Root Directory is `.` instead of `web` | set it to `web` (unless you are doing Appendix A) |
| Contact list empty after a schema bump | migration dropped the derived tables | `npm run db:rebuild-contacts` |
