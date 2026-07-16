# GitHub profile scraper

Scrapes GitHub users — and the portfolio sites they link from their profile —
for contact emails, keeping only people located in the **US, North America
(Canada, Mexico), Europe, or South America**. Shares the HTTP, email, and JSONL
helpers in [`../common/`](../common).

## How it works

```
search/users?q=location:"Berlin" followers:25..99   → logins
        → /users/<login>                            → profile + location
        → region filter (regions.py)                → US / North America / Europe / South America
        → 4 email sources                           → keep if any email found
```

Users are enumerated with GitHub's user-search API by **location**, walking every
country and city term in [`regions.py`](regions.py). Search returns at most 1000
hits per query, so each location is also sharded by follower count — six disjoint
buckets that together cover everyone. That is what turns "Germany" from 1000
reachable users into several thousand.

**The walk is breadth-first, and deliberately so.** Three things arrange it:

- the **follower shard is the outer loop** — it sweeps every country at
  `followers:>=500`, then every country at `100..499`, and so on down;
- the **locations are interleaved by country** — every country's name, then
  every country's biggest city, then their second city, and so on;
- each query gives up only **`--pages` pages (100 users each) per sweep** — the
  walk takes the first slice of *every* query before any query gets its second,
  then sweeps again a page deeper, until the queries run dry or you have enough.

Walked the obvious way instead — one location exhausted, then the next — a single
country swallows the whole run: Brazil is a name plus 16 cities, each six shards
of up to 1000 users, so a walk that starts there does not reach Europe for days.
(This is not hypothetical; it is what the first run of this scraper did.) Every
run stops early against the rate limits below, so what matters is that the part
it *did* walk is spread across all 58 countries rather than sunk into one. Sweeping
by shard also means the most-followed people in each country are seen first, which
is the half of any country worth having; the tail of small cities is what goes
unwalked, which is the right thing to lose.

For each user, emails are collected from four places, ranked best-first:

| source | where | notes |
|--------|-------|-------|
| `profile` | the public `email` field | rare, but it is an explicit invitation |
| `site` | the profile's `blog` URL | homepage, then `/contact` and `/about`; reads `mailto:` links and un-obfuscates `me [at] site [dot] com` |
| `readme` | `<login>/<login>` profile README + bio | whatever they typed themselves |
| `commits` | author email on their own recent public commits | set unless they enabled *keep my email private* — the highest-yield source |

The **primary** email (what the app offers to write to) is the most personal one
found: role addresses (`support@`, `info@`, `hello@`, …) are ranked below personal
ones regardless of source, so someone's own commit address beats the support desk
on their company site. `@users.noreply.github.com` and bot addresses are dropped.

Each record also carries the two links you'll want for outreach — the GitHub
profile (`url`) and the portfolio site (`site_url`) — plus `last_activity`, the
most recent of the user's public event feed (which catches work on *other*
people's repos, ~90-day window) and their newest repo push. The app shows and
sorts by that date, so the freshest contacts float to the top.

A user is kept only if they land in a target region **and** have at least one
email — same rule as the other scrapers here: a contact without an address is not
a contact.

No external dependencies — Python 3 standard library only.

## Token

Not strictly required, but a run without one is impractical: unauthenticated
search is **10 requests/minute** and the core API **60/hour**, versus 30/min and
5000/hour with a token. Create one at
<https://github.com/settings/tokens> — **no scopes needed**, it only reads public
data — and put it in the repo-root `.env`:

```
GITHUB_TOKEN=ghp_...
```

The scraper reads it from the environment or from `.env` (so it works both
standalone and when launched by `server.py`). Each user costs roughly 3–5 API
calls, so a token affords on the order of 1000–1500 users per hour.

## Usage

```bash
# scrape until you have 1000 users, spread across every country (resumable)
python scrapers/github/github_scrape.py --target 1000

# each run explores a different, random slice of the world -- so repeated runs
# keep finding fresh users instead of re-walking the same first queries
python scrapers/github/github_scrape.py --shuffle --limit 200

# a targeted scrape: males in Brazil whose accounts are 5+ years old
python scrapers/github/github_scrape.py \
    --countries Brazil --age-min 5 --gender male --limit 100

# scrape every target region (long-running, resumable) — the default
python scrapers/github/github_scrape.py

# just Canada + Mexico, first 200 users with an email
python scrapers/github/github_scrape.py --regions north_america --limit 200

# resume a stopped run at a given location term
python scrapers/github/github_scrape.py --start-location Berlin

# scrape named users directly (no search) — handy for testing
python scrapers/github/github_scrape.py --users torvalds,kentcdodds
```

Output is appended to `users.jsonl` (one user per line, **resumable** — re-running
skips logins already written). A pretty `users.json` array is also written when
the set is small enough.

| flag | default | meaning |
|------|---------|---------|
| `--target` | `0` | keep sweeping until the output holds this many users **in total**, counting the ones already in it (`0` = off) |
| `--limit` | `0` | max users to keep **this run** (`0` = no limit) |
| `--pages` | `2` | search pages (100 users each) per query **per sweep** — small keeps the walk broad; `0` drains each query's full 1000 before moving on |
| `--shuffle` | off | walk the queries in a **random order**, so each run explores a different slice of the world instead of re-covering the same first queries. Weighted toward the Americas (US, Canada/Mexico, South America) while keeping Europe woven in — see `REGION_WEIGHTS`. Ignores the resume cursor. The UI's Rescrape uses this by default |
| `--seed` | – | seed for `--shuffle`, to reproduce a run's order |
| `--countries` | – | keep only these countries (names or ISO codes, comma-separated), e.g. `Brazil,Argentina` or `BR,CA` — a finer net than `--regions` |
| `--age-min` / `--age-max` | – | keep only accounts within an age band (years since joining GitHub; min inclusive, max exclusive) |
| `--gender` | – | keep only `male` or `female`, inferred by Claude for near-keepers (needs `ANTHROPIC_API_KEY`) |

The narrowing filters are applied cheapest-first: **country** and **age** come
straight off the one profile fetch, so non-matching users are dropped *before*
the expensive email crawl. **Gender** is inferred by Claude and so runs last —
only for someone who already cleared every other filter and has an email, so a
plain scrape (no `--gender`) makes no Claude calls at all. Country/age/gender
rejections are **not** written to the skip list (they are relative to this run's
flags — a later run with different flags should still see those people).
| `--regions` | `us,north_america,europe,south_america` | subset of the four target regions |
| `--out` | `users.jsonl` | resumable JSONL output |
| `--json-out` | `users.json` | pretty JSON array (small sets only) |
| `--delay` | `0.5` | seconds between users |
| `--search-delay` | `2.2` | seconds between search calls (30/min authenticated) |
| `--start-location` | – | resume at `followers:0..2\|Berlin` (as written to `--cursor-out`), or a bare location term |
| `--users` | – | comma-separated logins instead of searching |
| `--no-site` / `--no-commits` / `--no-readme` | – | switch off an email source |

## The region filter

GitHub has no structured country field — `location` is free text ("Berlin",
"SF Bay Area", "são paulo, brasil", "Remote"). [`regions.py`](regions.py) maps that
string to a country and region using country names, native names ("Deutschland",
"Brasil"), city names, and the `City, ST` shape for US states. Accents are folded,
so `Medellín` and `Medellin` both match.

The filter is an **allow-list**: anything unrecognised ("Remote", "Tokyo", blank)
classifies as *no region* and the user is skipped. Genuinely ambiguous place names
(Cambridge, Birmingham, San Jose, Santiago, Córdoba) are deliberately left out of
the tables — a user filed under the wrong country is worse than one missed.

Names that *are* shared resolve by specificity, strongest signal first:

1. **a country, state or province name** — "Waterloo, Belgium" is Belgian and
   "Vancouver, Washington" is American, whatever the city tables say;
2. **a province code** — "London, ON" is Ontario's London, not England's;
3. **a city name**, except that a city the US also has ("Vancouver, WA",
   "Waterloo, IA") yields to a US postal code, which says more than the city does;
4. **any state code** — `, CA` is California far more often than it is Canada, so
   it only decides once no city has spoken ("Toronto, CA" is still Canada).

The regions are `us`, `north_america` (Canada, Mexico), `europe` and
`south_america`. The US has one to itself so it can be targeted — or skipped —
independently of its neighbours. Russia and Turkey are transcontinental and are
**not** treated as European. To change any of this, edit the `COUNTRIES` table —
search terms and the filter are derived from the same data, so a country added
there is both searched and recognised.
