# Where every credential comes from

One page per secret: what it is, where to click to get it, what to paste it
into, and how to prove it works. Companion to [DEPLOY.md](DEPLOY.md), which
covers *where* things run; this covers *what to put in them*.

Every value below is already named in `.env` / `.env.example` (the API) and
`web/.env.example` (the frontend), with a comment explaining what it turns on.

---

## Contents

1. [What you actually need](#1-what-you-actually-need)
2. [Where each value goes](#2-where-each-value-goes)
3. [Generate yourself — `API_TOKEN`, `UI_USER`, `UI_PASS`](#3-generate-yourself--api_token-ui_user-ui_pass)
4. [Managed MySQL — the `MYSQL_*` block](#4-managed-mysql--the-mysql_-block)
5. [Anthropic — `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`](#5-anthropic--anthropic_api_key-anthropic_model)
6. [Gmail SMTP — the `SMTP_*` block](#6-gmail-smtp--the-smtp_-block)
7. [GitHub scraping token — `GITHUB_TOKEN` / `SCRAPE_GITHUB_TOKEN`](#7-github-scraping-token--github_token--scrape_github_token)
8. [GitHub dispatch token — `GH_REPO`, `GH_DISPATCH_TOKEN`](#8-github-dispatch-token--gh_repo-gh_dispatch_token)
9. [`API_BASE_URL` and the platform-set variables](#9-api_base_url-and-the-platform-set-variables)
10. [Optional and currently unused](#10-optional-and-currently-unused)
11. [Verify everything at once](#11-verify-everything-at-once)
12. [Rotation, exposure, and cost](#12-rotation-exposure-and-cost)

---

## 1. What you actually need

Not all of these are required. Work down the list and stop where your
deployment stops.

| Credential | Needed for | Free? | Time |
| --- | --- | --- | --- |
| `API_TOKEN` | any public deployment | yes — you generate it | 5 s |
| `UI_USER` / `UI_PASS` | the UI login | yes — you choose them | 5 s |
| `MYSQL_*` | any deployment at all | yes (Aiven free plan) | ~10 min |
| `ANTHROPIC_API_KEY` | message generation + enrichment | **paid**, prepaid credits | 5 min |
| `SMTP_*` | sending mail | yes (Gmail) | 5 min |
| `GITHUB_TOKEN` | a usable GitHub scrape | yes | 2 min |
| `GH_DISPATCH_TOKEN` | the Rescrape button after deploy | yes | 3 min |
| `API_BASE_URL` | the frontend | — comes from your API host | — |
| `HOST` / `PORT` | — | set by the platform, not by you | — |

**You already hold four of these.** Your local `.env` has working values for
`ANTHROPIC_API_KEY`, `SMTP_PASSWORD`, `GITHUB_TOKEN` and `HASHNODE_TOKEN` — so
§5, §6 and §7 are only relevant if you need to *rotate* them or create separate
ones for the deployment. The genuinely new ones are §3, §4 and §8.

---

## 2. Where each value goes

The same secret often has to be pasted into more than one place, and getting
this wrong is the commonest deployment failure. `API_TOKEN` in particular must
be **byte-identical** in two places or every request 401s.

| Value | Local `.env` | API host (HF/Koyeb/Render) | Vercel | GitHub repo secrets |
| --- | :---: | :---: | :---: | :---: |
| `MYSQL_HOST` / `PORT` / `USER` / `PASSWORD` / `DATABASE` | — | ✅ | — | ✅ |
| `MYSQL_SSL=1` | — | ✅ (in the Dockerfile) | — | ✅ |
| `API_TOKEN` | — | ✅ | ✅ **same value** | — |
| `API_BASE_URL` | — | — | ✅ | — |
| `UI_USER` / `UI_PASS` | — | — | ✅ | — |
| `HOST=0.0.0.0` | — | ✅ (in the Dockerfile) | — | — |
| `ANTHROPIC_API_KEY` | ✅ | ✅ | — | ✅ |
| `ANTHROPIC_MODEL` | ✅ | ✅ | — | — |
| `SMTP_*` / `MAIL_FROM*` | ✅ | ✅ | — | — |
| `GITHUB_TOKEN` | ✅ | — | — | ✅ as `SCRAPE_GITHUB_TOKEN` |
| `GH_REPO` / `GH_DISPATCH_TOKEN` | — | ✅ | — | — |

Two things worth reading twice:

- **`GITHUB_TOKEN` must be named `SCRAPE_GITHUB_TOKEN` in GitHub Actions.**
  `GITHUB_TOKEN` is reserved by Actions and cannot be created as a secret.
  `.github/workflows/scrape.yml` maps it back to `GITHUB_TOKEN` inside the job.
- **Nothing goes in `web/.env.local` for a deployment** — Vercel's dashboard
  replaces that file. It is only for local development.

---

## 3. Generate yourself — `API_TOKEN`, `UI_USER`, `UI_PASS`

No account, no website. These are yours to invent.

```bash
python -c "import secrets; print('API_TOKEN =', secrets.token_urlsafe(32))"
python -c "import secrets; print('UI_PASS   =', secrets.token_urlsafe(18))"
```

`UI_USER` is any name you like (`admin`, your first name — it is not an email).

**Where they go:** `API_TOKEN` on the API host **and** on Vercel, identical.
`UI_USER`/`UI_PASS` on Vercel only.

**What they do:** `API_TOKEN` gates every API request via `X-Api-Token`
(`server.py`, `Handler._authorised`). `UI_USER`/`UI_PASS` drive the Basic-auth
login in `web/middleware.ts`. Leave all three unset locally — the server binds
loopback and the middleware stays off, which is what you want on a laptop.

---

## 4. Managed MySQL — the `MYSQL_*` block

### 4.1 Create the service (Aiven)

1. Go to **[console.aiven.io](https://console.aiven.io)** → sign up. No card
   for the free plan.
2. **Create service** → **MySQL**.
3. **Select service plan → Free** (1 CPU, 1 GB RAM, 5 GB storage).
4. **Pick a cloud region.** Write it down — your API host must be in the same
   region, and this is the single biggest performance decision you will make
   here.
5. Name the service and **Create**. It takes a few minutes to reach *Running*.

### 4.2 Read the values off the Overview tab

Aiven shows a **Connection information** panel. Map it across:

| Aiven field | Your variable |
| --- | --- |
| Host | `MYSQL_HOST` |
| Port | `MYSQL_PORT` |
| User | `MYSQL_USER` (usually `avnadmin`) |
| Password (click the eye) | `MYSQL_PASSWORD` |
| Database name | `MYSQL_DATABASE` (`defaultdb`, or make your own) |
| — | `MYSQL_SSL=1` |

Aiven's certificate chains to a public root, so `MYSQL_SSL=1` is enough and you
do **not** need the CA file. Download **CA certificate** and use
`MYSQL_SSL_CA=/app/ca.pem` instead only if your host's image turns out to have
no system trust store.

### 4.3 Two settings to change before you import

- **Create the project's own database** (optional but tidier than `defaultdb`)
  from the console's **Query editor**:

  ```sql
  CREATE DATABASE email_scrapper
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  ```

- **Raise the packet limit**, or your ~52 MB import dies part-way:
  **Service settings → Advanced configuration → Add config option →
  `mysql.max_allowed_packet` → `67108864`** → Apply.

### 4.4 Verify

```bash
MYSQL_HOST=... MYSQL_PORT=... MYSQL_USER=avnadmin MYSQL_PASSWORD=... \
MYSQL_DATABASE=email_scrapper MYSQL_SSL=1 \
python -c "import db; db.bootstrap(); print(db.table_counts())"
```

An empty-but-created set of tables means the credentials and TLS both work.
`(2003, "Can't connect")` almost always means `MYSQL_SSL=1` is missing.

**TiDB Cloud instead:** same shape, 25 GB free instead of 5 GB, MySQL-compatible
rather than MySQL. Create a **Starter** cluster and take the connection string.

---

## 5. Anthropic — `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`

### 5.1 Create the key

1. **[console.claude.com](https://console.claude.com)** → sign in.
2. **Settings → API keys** → **Create Key**.
3. Name it (`email-scrapper-prod`) and pick the workspace. Keys are scoped to a
   workspace, which is how you keep this project's spend separate from anything
   else.
4. **Copy it now** — the full key is shown once and never again. It starts
   `sk-ant-api03-`.

### 5.2 Add credits

The API is prepaid — a key with no balance returns 400 on every call.
**Plans & Billing → Credits → Add credits.** Start with $20 for this workload.

### 5.3 `ANTHROPIC_MODEL`

Your `.env` currently has `claude-opus-5`, which is correct and current. The
values worth knowing:

| Model | ID | Input / output per 1M tokens |
| --- | --- | --- |
| Claude Opus 5 | `claude-opus-5` | $5 / $25 |
| Claude Sonnet 5 | `claude-sonnet-5` | $3 / $15 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1 / $5 |

Use the exact ID strings — no date suffixes.

**One variable drives two features.** `server.py` uses `ANTHROPIC_MODEL` for
both outreach-message generation and country/gender enrichment, so dropping to
Haiku to save on enrichment also changes the quality of the emails you send.
That is a real trade-off, not a free win.

**What enrichment costs, roughly.** Each contact is one small call — a few
hundred input tokens, capped at 200 output. Call it ~$0.006 per contact on Opus
5. You have 12,794 contacts with 2,059 already cached, so a full backfill of
the rest lands somewhere near **$60–70**. On Haiku 4.5 the same sweep is closer
to $12. Set `ENRICH=0` to spend nothing until you decide.

### 5.4 Verify

```bash
curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-5","max_tokens":16,
       "messages":[{"role":"user","content":"Say OK."}]}'
```

A JSON response with `"type":"message"` means key + credits + model ID are all
good. `authentication_error` = bad key; `invalid_request_error` mentioning
credit = empty balance; `not_found_error` = wrong model ID.

Or just start the server — it prints `Messaging: generate=on` when the key
arrived, `generate=OFF` when it did not.

---

## 6. Gmail SMTP — the `SMTP_*` block

An app password is **not** your Google password. It is a 16-character
credential that bypasses 2FA for one application.

1. **2-Step Verification must be on first.**
   [myaccount.google.com](https://myaccount.google.com) → **Security** →
   **2-Step Verification** → turn on. App passwords do not exist without it.
2. Go to **[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)**
   (it is not linked prominently from the Security page any more).
3. Type a name — `email-scrapper` — and **Create**.
4. Copy the 16 characters. Google shows them in four groups of four; the
   spaces are display only. Quote it in `.env` either way:
   `SMTP_PASSWORD="abcd efgh ijkl mnop"`.

Fill in the rest:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com          # the account that made the app password
SMTP_PASSWORD=<the 16 characters>
MAIL_FROM=you@gmail.com          # must match SMTP_USER for Gmail
MAIL_FROM_NAME=Ephrem            # the display name recipients see
```

Three things that bite people here:

- **The app password must be generated on the same account as `SMTP_USER`.**
  Your `.env` notes this already for `asefaephrem000@gmail.com`.
- **Outlook/Hotmail personal accounts cannot do this at all** — Microsoft
  permanently disabled SMTP password auth (error 5.7.139).
- **Gmail caps you at ~500 recipients/day** (2,000 on Workspace). A real
  outreach campaign needs a transactional provider — Resend and Brevo both have
  free tiers — which is a change to `SMTP_HOST`/`USER`/`PASSWORD`, nothing more.

**Verify:** the server prints `send=on` at startup, and the honest test is to
send yourself a message from the UI.

---

## 7. GitHub scraping token — `GITHUB_TOKEN` / `SCRAPE_GITHUB_TOKEN`

Read-only, public data only. Without one, GitHub search allows 10 requests a
minute and the REST API 60 an hour, which makes a real scrape pointless; with
one it is 30/minute and 5,000/hour.

1. **[github.com/settings/tokens](https://github.com/settings/tokens)** →
   **Generate new token** → **classic** is fine here.
2. Name it, set an expiry (90 days is a reasonable default).
3. **Tick no scopes at all.** It only reads public profiles. An unscoped
   classic token still lifts the rate limit, which is the entire point.
4. Generate and copy — shown once.

**Where it goes:** `GITHUB_TOKEN` in local `.env`; as a repository secret named
**`SCRAPE_GITHUB_TOKEN`** (Settings → Secrets and variables → Actions). The
rename is forced: Actions reserves `GITHUB_TOKEN` and refuses to let you create
a secret with that name. `scrape.yml` maps it back for the scrapers.

**Verify:**

```bash
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/rate_limit | python -m json.tool | head -12
```

`"limit": 5000` means it is working. `"limit": 60` means the header did not
reach GitHub.

---

## 8. GitHub dispatch token — `GH_REPO`, `GH_DISPATCH_TOKEN`

This is the one that makes the **Rescrape** button work after deployment. It is
a *different, more privileged* token from §7 — keep them separate so the
read-only scraping token stays read-only.

`GH_REPO` is just `beeva/email-scrapper` — owner/name, no URL.

For the token, use a **fine-grained** PAT so it can touch this repository only:

1. **[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)**
2. **Token name:** `email-scrapper-dispatch`. **Expiration:** 90 days.
3. **Resource owner:** `beeva`.
4. **Repository access → Only select repositories → `email-scrapper`.**
   Do not pick "All repositories".
5. **Repository permissions → Actions → Read and write.**
   (Metadata → Read-only is added automatically and is required.)
   Nothing else. It does not need code, contents, or workflow permissions.
6. **Generate token** and copy — shown once.

**Where it goes:** the API host only, as `GH_REPO` + `GH_DISPATCH_TOKEN`.
Never Vercel, never a repo secret.

**Verify** — this triggers a real scrape, so use a small limit:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $GH_DISPATCH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/beeva/email-scrapper/actions/workflows/scrape.yml/dispatches \
  -d '{"ref":"main","inputs":{"source":"phones","args":"--limit 5"}}'
```

`204` is success (that endpoint returns no body). `401` = bad token. `404` =
wrong repo, the workflow file is not on `main` yet, or the token lacks Actions
write. The same three codes come back through the UI as
`GitHub refused the dispatch (…)`.

---

## 9. `API_BASE_URL` and the platform-set variables

**`API_BASE_URL`** is not a credential — it is wherever your API landed:

| Host | Looks like |
| --- | --- |
| Hugging Face Space | `https://<you>-email-scrapper-api.hf.space` |
| Koyeb | `https://<app>-<org>.koyeb.app` |
| Render | `https://<name>.onrender.com` |

No trailing slash needed (`web/lib/emails.ts` strips it). Goes on Vercel only.

**`HOST` and `PORT` are set for you.** The `Dockerfile` sets `HOST=0.0.0.0` and
`PORT=7860`; Render and Koyeb inject their own `$PORT`, which `server.py`
prefers. Leave both blank in `.env` — a non-blank `HOST` on your laptop would
expose the API to your whole network.

---

## 10. Optional and currently unused

| Variable | Status |
| --- | --- |
| `HASHNODE_TOKEN` | **Nothing reads it.** `scrapers/hashnode/` holds only a stale `.cursor` file — the scraper itself is not in this tree. Harmless to keep; delete it if you like. |
| `SLACK_TOKEN` | Read only by `scrapers/slack/emails.py`, a standalone script. Not needed by the server or any workflow. From [api.slack.com/apps](https://api.slack.com/apps) → your app → OAuth & Permissions → User OAuth Token, with `users:read` and `users:read.email`. |
| `MYSQL_BASEDIR` / `MYSQL_DATADIR` / `MYSQL_BUFFER_POOL` / `MYSQL_MAX_PACKET` | Local-only. They point `scripts/mysql-server.js` at XAMPP's `mysqld`; a managed database ignores all four. |
| `ENRICH` / `ENRICH_WORKERS` | Not secrets — `ENRICH=0` disables Claude enrichment, `ENRICH_WORKERS=2` is the right value on a small free container. |
| `GH_WORKFLOW` / `GH_REF` | Not secrets. Defaults `scrape.yml` and `main` are already correct. |

---

## 11. Verify everything at once

The startup banner checks four credentials in one line each — it is the fastest
way to find the one you got wrong:

```
[db] avnadmin@mysql-xxx.aivencloud.com:12345/email_scrapper ready   ← MYSQL_* + MYSQL_SSL
Loaded 12794 contacts (discourse=589, aboutme=410, github=11795)     ← the data arrived
Messaging: generate=on, send=on, from=you@gmail.com                  ← ANTHROPIC_API_KEY, SMTP_PASSWORD
Enrichment: on (2059 cached, 2 workers)                              ← ENRICH
Scrapes run in: GitHub Actions (beeva/email-scrapper)                ← GH_REPO + GH_DISPATCH_TOKEN
Auth: on (X-Api-Token)                                               ← API_TOKEN
Serving on http://0.0.0.0:7860
```

| Banner says | Missing |
| --- | --- |
| `generate=OFF (set ANTHROPIC_API_KEY)` | `ANTHROPIC_API_KEY` |
| `send=OFF (set SMTP_PASSWORD)` | `SMTP_PASSWORD` |
| `Scrapes run in: this process` | `GH_REPO` or `GH_DISPATCH_TOKEN` |
| `Auth: off (local only)` | `API_TOKEN` |
| `Enrichment: off` | `ANTHROPIC_API_KEY`, or `ENRICH=0` |
| never reaches `[db] … ready` | the `MYSQL_*` block, or `MYSQL_SSL=1` |

Then the two-sided checks:

```bash
curl -s -o /dev/null -w 'no token  → %{http_code}\n' https://your-api/api/db/status
curl -s -o /dev/null -w 'token     → %{http_code}\n' \
     -H "X-Api-Token: $API_TOKEN" https://your-api/api/db/status
curl -s -o /dev/null -w 'UI no auth → %{http_code}\n' https://your-app.vercel.app/
```

Expect `401`, `200`, `401`. A `401` on the second line means the two
`API_TOKEN` values differ — compare them character by character rather than
re-generating.

---

## 12. Rotation, exposure, and cost

**Rotate anything that has been in a shell history, a screenshot, a chat, or a
commit.** All four of your current secrets have been in this conversation, so
treat them as known:

| Secret | Revoke at |
| --- | --- |
| `ANTHROPIC_API_KEY` | console.claude.com → Settings → API keys → delete |
| `GITHUB_TOKEN` | github.com/settings/tokens → Delete |
| `GH_DISPATCH_TOKEN` | github.com/settings/personal-access-tokens → Revoke |
| `SMTP_PASSWORD` | myaccount.google.com/apppasswords → trash icon |
| `MYSQL_PASSWORD` | Aiven console → Service → Users → Reset password |
| `API_TOKEN` / `UI_PASS` | regenerate (§3) and update both places |

Rotation is per-credential — revoking a GitHub token does not affect your
account, and a new Gmail app password does not invalidate the others.

**Hygiene that actually matters here:**

- **Git history is clean** — checked on 2026-08-11: `.env` has never been
  committed, and the only `ghp_` string in the history is the `ghp_...`
  placeholder in `scrapers/github/README.md`. Re-check after any close call
  with `git log --all -p -- .env` (expect no output) and
  `git log --all -S "sk-ant-api03" --oneline`.
- Use each platform's secret store — Aiven, Hugging Face **Settings → Variables
  and secrets**, Vercel **Environment Variables**, GitHub **Actions secrets**.
  Never bake a secret into the Docker image; `.dockerignore` excludes `.env`
  for exactly this reason.
- Set expiries on both GitHub tokens. A 90-day expiry that breaks a scrape is a
  much better outcome than a token that lives forever.
- Keep the repository **private** while it holds dumps or artifacts of scraped
  personal data.

**What each one can cost you if it leaks:** the Anthropic key spends real
credits; the Gmail app password sends mail as you and burns your sending
reputation; `GH_DISPATCH_TOKEN` starts Actions runs; `MYSQL_PASSWORD` and
`API_TOKEN` both expose the whole contact archive. Only the scraping token is
low-stakes — it reads public data and nothing else.

**Ongoing spend:** the database, the API container, the frontend and Actions
are all free. Anthropic is the only line item, and `ENRICH=0` reduces it to
whatever message generation you actually use.
