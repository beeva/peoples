#!/usr/bin/env python3
"""Scrape GitHub users -- and their portfolio sites -- for contact emails.

Users are enumerated with GitHub's user-search API by *location*, walking the
country and city terms in ``regions.py`` (US, North America, Europe, South
America). Search caps any one query at 1000 results, so each location is
additionally sharded by follower count -- that is what turns "Germany" from
1000 users into ~6000.

The terms come back interleaved by country (every country's name, then every
country's biggest city, ...), so a run that stops early -- and against these
rate limits they all do -- has still covered every country rather than
exhausting the first one. See ``search_terms`` in ``regions.py``.

Phone / WhatsApp numbers are collected in the same pass: the site and README
are fetched for the email anyway, so they are scanned for a number too, at no
extra request. Only numbers something vouches for are kept -- a wa.me/tel: link,
a "WhatsApp:" label, or a leading + and a real country code (see
``common/phones.py``).

For every user we then look for an email in four places, best first:

  1. profile   -- the public `email` field (rare, but it is a deliberate invite)
  2. site      -- the profile's `blog` URL: homepage + /contact + /about, incl.
                  `mailto:` links and "name [at] domain [dot] com" obfuscation
  3. readme    -- the <login>/<login> profile README and the bio
  4. commits   -- the author email on their own recent public commits, which is
                  set unless they enabled "keep my email private"

Each kept user also records `last_activity` -- the most recent of their public
event feed (catches activity on other people's repos, ~90-day window) and their
newest repo push -- so the app can show and sort by how recently they were seen.

Only users that land in a target region AND are contactable are kept -- an
email **or** a phone / WhatsApp number, which matches the other scrapers in this
repo (a contact you cannot reach is not a contact). Users go straight into the
database, keyed by login, and a run asks it what it already holds: re-running
skips the users already stored, so a stopped run costs nothing to restart.

A GITHUB_TOKEN is effectively required -- unauthenticated search is 10 requests
per minute (vs 30) and the core API 60 per hour (vs 5000). Create one at
https://github.com/settings/tokens; no scopes are needed for public data. It is
read from the environment or the repo-root `.env`.

Usage:
    python scrapers/github/github_scrape.py [--target 1000] [--pages 2]
                       [--limit 200] [--regions us,north_america]
                       [--out users.jsonl] [--delay 0.5] [--start-location Berlin]

    --target 1000  keep sweeping until the output holds 1000 users in total
                   (counting the ones already in it). 0 = off, the default.
    --limit 0      max users to keep this run. 0 = no limit, the default.
    --pages 2      search pages (100 users each) per query per sweep -- the knob
                   that trades breadth for depth. 0 drains each query in full.
    --shuffle      walk the queries in a random order, so each run explores a
                   different slice of the world instead of re-covering the same
                   first queries. The way to keep finding fresh users on restart.

No external dependencies -- standard library plus scrapers/common/.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Make the shared `common` package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (  # noqa: E402
    EMAIL_PROVIDERS,
    email_provider,
    email_rank,
    extract_emails,
    fetch,
    load_env,
)
from common.phones import extract_phones, merge_phones, phone_first  # noqa: E402
from common.store import RecordStore, SkipStore  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regions import ALL_REGIONS, REGIONS, classify, search_terms  # noqa: E402

API = "https://api.github.com"
UA = "peoples (polite; github profile scraper)"
SCRIPT_DIR = Path(__file__).resolve().parent

load_env()
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

# Claude, used only for the optional --gender filter (see infer_gender). Read
# from the same .env the server uses, so the two agree on model and key.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
SECONDS_PER_YEAR = 365.25 * 86400

# Follower shards. Search returns at most 1000 hits per query, so each location
# is asked six narrower questions instead of one broad one. The buckets are
# disjoint and cover everyone, so no user is seen twice or missed.
FOLLOWER_BUCKETS = (
    "followers:>=500", "followers:100..499", "followers:25..99",
    "followers:10..24", "followers:3..9", "followers:0..2",
)
SEARCH_PAGE_CAP = 10          # 10 pages x 100 = the API's hard 1000-result cap
COMMIT_REPOS = 3              # newest non-fork repos to check for a commit email

# Rejection reasons stable enough to persist to the skip list. The fine-grained
# filters (country/age/gender/provider) are left out on purpose: they change run
# to run, so a "no" under one run's flags must not hide the person from the next.
PERSISTED_SKIP_REASONS = {"not-a-user", "no-contact", "off-region"}

# How hard --shuffle leans on each region. The Americas are the focus, so their
# queries tend to land earlier in the random order; Europe is weighted lower but
# never zero, so it stays woven through the walk rather than pushed to the end.
# A weight of 0 would drop a region entirely -- deliberately not done here.
REGION_WEIGHTS = {"us": 3.0, "north_america": 3.0, "south_america": 3.0,
                  "europe": 1.0}
DEFAULT_REGION_WEIGHT = 1.0

# Emails that are real addresses but not contactable people.
NOREPLY_RE = re.compile(
    r"(@users\.noreply\.github\.com$|^no-?reply@|@example\.com$"
    r"|^git@|^action@github\.com$|\+.*@users\.noreply)", re.I
)
# How much we trust each source to be the address the person wants to be reached
# at: a profile email is an invitation; a commit email is merely a fact.
SOURCE_RANK = {"profile": 0, "site": 1, "readme": 2, "commits": 3}
# "name [at] domain [dot] com" / "name (at) domain (dot) com" / "name at domain dot com"
OBFUSCATED_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+)\s*"
    r"([A-Za-z0-9.-]+?)\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*([A-Za-z]{2,})\b",
    re.I,
)
# Sites that never yield a personal email and dislike being crawled -- if the
# `blog` field points at one of these, we do not follow it.
SKIP_HOST_RE = re.compile(
    r"(^|\.)(github\.com|gist\.github\.com|linkedin\.com|twitter\.com|x\.com"
    r"|facebook\.com|instagram\.com|youtube\.com|t\.me|medium\.com"
    r"|stackoverflow\.com|npmjs\.com)$", re.I
)
CONTACT_PATHS = ("/contact", "/about", "/contact.html", "/about.html")


# ---- GitHub API -----------------------------------------------------------
class RateLimited(Exception):
    """Raised when the hourly/secondary limit is exhausted and waiting is futile."""


def _sleep_until(reset_epoch: float, why: str) -> None:
    wait = max(1.0, min(3600.0, reset_epoch - time.time() + 2))
    print(f"  … {why}; sleeping {int(wait)}s", file=sys.stderr)
    time.sleep(wait)


def gh_get(url: str, *, tries: int = 4):
    """GET a GitHub API URL, honouring both the hourly and the secondary limit.

    Unlike ``common.fetch`` this needs the *response headers* -- GitHub signals
    its budget in X-RateLimit-Remaining/Reset, and the polite thing (and the
    thing that keeps a long run alive) is to sleep until the window resets
    rather than hammer into a 403.
    """
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                body = resp.read()
            # Pre-emptively wait out the window when the budget is spent, so the
            # next call does not have to fail first.
            if remaining is not None and remaining.isdigit() and int(remaining) == 0:
                _sleep_until(float(reset or 0), "rate-limit budget spent")
            return json.loads(body or b"null")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                retry_after = (e.headers.get("Retry-After") or "") if e.headers else ""
                reset = (e.headers.get("X-RateLimit-Reset") or "") if e.headers else ""
                if retry_after.isdigit():
                    _sleep_until(time.time() + int(retry_after), "secondary rate limit")
                elif reset.isdigit():
                    _sleep_until(float(reset), "rate limited")
                else:
                    time.sleep(60)
                continue
            if e.code in (404, 409, 451):  # gone / empty repo / blocked account
                return None
            if e.code >= 500 and attempt < tries:
                time.sleep(2 * attempt)
                continue
            raise
        except (OSError, json.JSONDecodeError):
            # OSError covers URLError, timeouts and the connection resets GitHub
            # hands out mid-run; all are worth one more try.
            if attempt == tries:
                raise
            time.sleep(1.5 * attempt)
    raise RateLimited(f"gave up on {url}")


def search_logins(term: str, bucket: str, pages, delay: float) -> tuple[list[str], bool]:
    """Logins for one `location: + followers:` query, over `pages` only.

    Returns (logins, more). ``more`` is False once GitHub has run out of results
    for this query -- a short page means the last one -- so the caller can retire
    the query instead of paying a search call to re-ask it on the next, deeper
    sweep. Only `pages` are fetched: a sweep takes the first slice of every
    query before any query gets its second, which is what keeps the walk broad.
    """
    logins: list[str] = []
    for page in pages:
        if page > SEARCH_PAGE_CAP:      # the API serves no more than 1000 hits
            return logins, False
        q = urllib.parse.quote(f'location:"{term}" {bucket} type:user')
        url = (f"{API}/search/users?q={q}&per_page=100&page={page}"
               f"&sort=joined&order=desc")
        res = gh_get(url)
        items = (res or {}).get("items") or []
        logins += [item["login"] for item in items if item.get("login")]
        if len(items) < 100:            # ran out -- nothing deeper to come back for
            return logins, False
        time.sleep(delay)
    return logins, True


# ---- email sources --------------------------------------------------------
def _clean(emails) -> list[str]:
    """Drop bots, noreply and placeholder addresses; lowercase + dedupe."""
    out = []
    for email in emails:
        email = (email or "").strip().lower()
        if not email or NOREPLY_RE.search(email):
            continue
        if email not in out:
            out.append(email)
    return out


def deobfuscated_emails(text: str) -> list[str]:
    """Recover addresses people write as 'me [at] site [dot] com' to dodge bots."""
    found = []
    for user, domain, tld in OBFUSCATED_RE.findall(text or ""):
        candidate = f"{user}@{domain}.{tld}".lower()
        if candidate not in found:
            found.append(candidate)
    return found


def site_url_of(blog: str) -> str:
    """Normalise the profile's `blog` field into a fetchable URL ('' to skip).

    People routinely put non-URLs in `blog` -- a bare email, an @handle. A bare
    email is the trap: prefixing it with https:// yields a URL whose "host" is
    the mail domain (`https://me@gmail.com` -> host gmail.com), so it has to be
    rejected explicitly rather than caught by the host check.
    """
    blog = (blog or "").strip()
    if not blog or blog.startswith("mailto:") or blog.startswith("@"):
        return ""
    # A bare email (no scheme, has an @ before any '/') is not a site.
    if not re.match(r"^https?://", blog, re.I) and "@" in blog.split("/", 1)[0]:
        return ""
    if not re.match(r"^https?://", blog, re.I):
        blog = f"https://{blog}"
    try:
        parsed = urllib.parse.urlparse(blog)
    except ValueError:
        return ""
    host = parsed.hostname or ""
    # userinfo in the authority (user:pass@ or user@) means it wasn't a plain URL.
    if not host or parsed.username or SKIP_HOST_RE.search(host) or "." not in host:
        return ""
    return blog


def scrape_site(url: str, delay: float) -> tuple[list[str], list[dict]]:
    """Fetch a portfolio site and pull emails *and* phone numbers from it.

    The homepage first; only if it yields nothing do we try /contact and /about,
    which is where a personal site usually hides the address. Failures are not
    fatal -- a dead or hostile site just means no contacts from this source.

    Phones are harvested from the same pages, in the same order, with the same
    early exit -- so this makes exactly as many requests as it did when it only
    looked for emails. The page is already in memory; not scanning it for a
    number would just mean fetching it again later.
    """
    phones: list[dict] = []

    def contacts_at(target: str) -> list[str]:
        nonlocal phones
        try:
            html = fetch(target, parse="text", ua=UA, tries=2, timeout=20,
                         none_on=(400, 401, 403, 404, 405, 410, 451))
        except Exception:  # noqa: BLE001 -- a broken personal site is routine
            return []
        if not html:
            return []
        # Raw HTML, not stripped text: wa.me and tel: live in href attributes,
        # which is the strongest evidence a number is real.
        phones = merge_phones(phones, extract_phones(html))
        return extract_emails(html) + deobfuscated_emails(html)

    found = contacts_at(url)
    if found:
        return found, phone_first(phones)
    base = url.rstrip("/")
    for path in CONTACT_PATHS:
        time.sleep(delay)
        found = contacts_at(base + path)
        if found:
            return found, phone_first(phones)
    return [], phone_first(phones)


def user_repos(login: str) -> list[dict]:
    """The user's own repos, most recently pushed first.

    Fetched once per user and reused: it feeds both the commit-email walk and
    the last-activity date, and an API call saved is a user scraped.
    """
    return gh_get(f"{API}/users/{login}/repos"
                  f"?sort=pushed&per_page=10&type=owner") or []


def last_activity(login: str, repos: list[dict]) -> str:
    """When this person was last publicly active on GitHub (ISO8601, '' if never).

    The events feed is the real answer -- it catches someone whose own repos are
    stale but who reviews, comments and pushes to other people's projects daily,
    which is most working developers. It only retains ~90 days, though, so a
    quieter account falls back to its newest repo push, and finally to the
    profile's own `updated_at`.
    """
    events = gh_get(f"{API}/users/{login}/events/public?per_page=1") or []
    stamps = [e.get("created_at") or "" for e in events]
    stamps += [r.get("pushed_at") or "" for r in repos]
    return max(stamps) if any(stamps) else ""


def commit_emails(login: str, repos: list[dict]) -> list[str]:
    """Author emails from the user's own recent commits.

    Every git commit carries the author's email, and the commits endpoint serves
    it for public repos -- so anyone who never turned on GitHub's "keep my email
    private" setting is reachable this way. (The events API used to carry the
    same thing in its PushEvent payload and no longer does, hence the repo walk.)

    We stop at the first repo that yields a usable address: each repo costs an
    API call, and one good email is all we need.
    """
    found: list[str] = []
    for repo in [r for r in repos if not r.get("fork")][:COMMIT_REPOS]:
        name = repo.get("name")
        if not name:
            continue
        commits = gh_get(f"{API}/repos/{login}/{name}/commits"
                         f"?author={login}&per_page=5") or []
        for commit in commits:
            author = ((commit.get("commit") or {}).get("author") or {})
            email = (author.get("email") or "").strip()
            if email and email not in found:
                found.append(email)
        if _clean(found):  # a real address, not just @users.noreply -- done
            break
    return found


def readme_contacts(login: str) -> tuple[list[str], list[dict]]:
    """Emails and phone numbers typed into the <login>/<login> profile README."""
    url = f"https://raw.githubusercontent.com/{login}/{login}/HEAD/README.md"
    try:
        text = fetch(url, parse="text", ua=UA, tries=2, timeout=20,
                     none_on=(400, 403, 404, 410))
    except Exception:  # noqa: BLE001
        return [], []
    if not text:
        return [], []
    return (extract_emails(text) + deobfuscated_emails(text),
            extract_phones(text))


# ---- account age + gender (for the optional --age / --gender filters) -----
def account_age_years(created_at: str, now: float) -> float | None:
    """Years between the GitHub join date and now, or None if unparseable."""
    if not created_at:
        return None
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return max(0.0, (now - ts) / SECONDS_PER_YEAR)


def date_ts(value: str) -> float | None:
    """Timestamp of an ISO date/datetime string, or None if unparseable.

    Used by the --joined-*/--active-* filters; a bare date ('2024-06-01')
    parses as local midnight, which is plenty for a day-granular filter.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _in_date_bounds(ts: float | None, after: float | None,
                    before: float | None) -> bool:
    """after inclusive, before exclusive -- the app's date-filter contract.
    A missing date cannot clear a bound, matching the UI filters."""
    if ts is None or ts <= 0:
        return False
    if after is not None and ts < after:
        return False
    if before is not None and ts >= before:
        return False
    return True


_GENDER_SCHEMA = {
    "type": "object",
    "properties": {"gender": {"type": "string",
                              "enum": ["male", "female", "unknown"]}},
    "required": ["gender"],
    "additionalProperties": False,
}
_GENDER_CACHE: dict[str, str] = {}   # name (folded) -> gender, per run


def infer_gender(name: str, location: str = "", bio: str = "") -> str:
    """Claude's best guess at a person's gender: 'male' | 'female' | 'unknown'.

    Same model and framing as the server's enrichment, so a scrape-time guess
    and a later UI enrichment agree. Cached per name for the run, and a no-op
    ('unknown') without a key -- the caller decides what an unknown means for
    the filter. Any transport error yields 'unknown' rather than dropping a
    user over a network blip.
    """
    key = (name or "").strip().lower()
    if not key or not ANTHROPIC_API_KEY:
        return "unknown"
    if key in _GENDER_CACHE:
        return _GENDER_CACHE[key]
    system = (
        "You infer a person's likely gender from sparse signals: their name, "
        "any stated location, and text they wrote. Reply with JSON. gender = "
        "'male', 'female', or 'unknown'. A clearly gendered given name is "
        "enough; when signals are weak prefer 'unknown' over guessing."
    )
    user = (f"Name: {name or '(unknown)'}\n"
            f"Stated location: {location or '(none)'}\n"
            f"Text they wrote:\n{(bio or '')[:600]}")
    payload = {
        "model": ANTHROPIC_MODEL, "max_tokens": 60, "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema",
                                     "schema": _GENDER_SCHEMA}},
    }
    req = urllib.request.Request(
        ANTHROPIC_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json",
                 "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"})
    gender = "unknown"
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        g = str(json.loads(text).get("gender") or "unknown").lower()
        gender = g if g in ("male", "female", "unknown") else "unknown"
    except (OSError, ValueError, KeyError):
        gender = "unknown"   # a blip is not a verdict; let the user through
    _GENDER_CACHE[key] = gender
    return gender


# ---- one user -------------------------------------------------------------
def scrape_user(login: str, *, regions, site_delay: float,
                use_commits: bool = True, use_site: bool = True,
                use_readme: bool = True, countries=None,
                age_min=None, age_max=None, genders=None, providers=None,
                joined_after=None, joined_before=None,
                active_after=None, active_before=None,
                ) -> tuple[dict | None, str]:
    """Fetch a profile, region-filter it, and gather its emails.

    Returns (record, "") for a keeper, or (None, reason) for someone we do not
    want -- "not-a-user" (an org), "off-region", "off-country", "off-age",
    "off-joined", "off-activity", "no-contact", "off-provider", "off-gender".
    The reason is what the caller writes to the skip list, and it is only ever a
    *settled* verdict: a rate limit or a network blip raises instead, so a user
    is never written off for a reason that might not be true tomorrow.

    The filters are applied cheapest-first: region, then country, age and join
    date (all read straight off the one profile fetch), then last activity
    (one events call), and only then the expensive email gathering. The mailbox
    provider is judged on the address that gathering produced. Gender is last of
    all -- it needs a Claude call, so it runs only for someone who already
    cleared every other bar and has an email.
    """
    prof = gh_get(f"{API}/users/{login}")
    if not prof or prof.get("type") != "User":
        return None, "not-a-user"

    location = prof.get("location") or ""
    country = classify(location)
    if not country or country["region"] not in regions:
        return None, "off-region"

    # Country: a finer net than --regions, e.g. just Brazil within the Americas.
    if countries and country["name"].lower() not in countries \
            and country["code"].lower() not in countries:
        return None, "off-country"

    # Account age (years since the join date). Min inclusive, max exclusive, to
    # match the app's UI filter. An unparseable join date can't clear a bound.
    if age_min is not None or age_max is not None:
        age = account_age_years(prof.get("created_at") or "",
                                datetime.now(timezone.utc).timestamp())
        if age is None:
            return None, "off-age"
        if age_min is not None and age < age_min:
            return None, "off-age"
        if age_max is not None and age >= age_max:
            return None, "off-age"

    # Join date, as an absolute calendar window (the age filter's relative
    # cousin). Also read straight off the profile, so it costs nothing.
    if joined_after is not None or joined_before is not None:
        if not _in_date_bounds(date_ts(prof.get("created_at") or ""),
                               joined_after, joined_before):
            return None, "off-joined"

    bio = (prof.get("bio") or "").strip()
    site = site_url_of(prof.get("blog") or "")
    repos = user_repos(login)

    # Last public activity. This costs one events call, so it only runs when
    # an activity bound is actually set -- but the fetched date is then reused
    # for the record, so a filtered run pays nothing extra overall.
    last_act: str | None = None
    if active_after is not None or active_before is not None:
        last_act = last_activity(login, repos)
        if not _in_date_bounds(date_ts(last_act), active_after, active_before):
            return None, "off-activity"

    site_emails, site_phones = (scrape_site(site, site_delay)
                                if (use_site and site) else ([], []))
    readme_emails_found, readme_phones = (readme_contacts(login)
                                          if use_readme else ([], []))
    sources = {
        "profile": _clean([prof.get("email") or ""]),
        "site": _clean(site_emails),
        "readme": _clean(readme_emails_found
                         + extract_emails(bio) + deobfuscated_emails(bio)),
        "commits": _clean(commit_emails(login, repos)) if use_commits else [],
    }
    # Numbers from every page this scrape already fetched, best first. The bio
    # is included for the rare profile that carries one.
    phones = phone_first(merge_phones(site_phones, readme_phones,
                                      extract_phones(bio)))
    # Best address first, so the primary -- the one the UI offers to write to --
    # is the one a human actually reads. In order: a mailbox they own (gmail,
    # proton) beats a work address, which beats a role desk (support@, info@);
    # only then does it matter which source found it. That last part is the
    # tiebreak, not the rule: someone's own gmail from a commit outranks
    # careers@theiremployer.com sitting in their profile field.
    ranked = sorted(
        ((email_rank(email) + (SOURCE_RANK[src],), email)
         for src, found in sources.items() for email in found),
        key=lambda pair: pair[0],
    )
    emails = _clean([email for _, email in ranked])
    # A contact needs a way to reach them, not an email specifically: someone
    # who published only a WhatsApp number is every bit as contactable as
    # someone who published only an address.
    if not emails and not phones:
        return None, "no-contact"

    # Mailbox bucket of the address we would actually write to -- the same
    # judgement the app's Email filter makes, made here so a targeted run only
    # keeps, say, Gmail people. Someone kept for a number alone has no address
    # to bucket, so a provider filter rules them out: the filter is a statement
    # about where the mail would land.
    if providers and email_provider(emails[0] if emails else "") not in providers:
        return None, "off-provider"

    # Gender last: it costs a Claude call, so only ask for someone who already
    # cleared region/country/age and is contactable. Stored on the record so the
    # app can show it without inferring again. When no gender filter is set this
    # never runs -- a plain scrape pays nothing for Claude.
    gender = ""
    if genders:
        gender = infer_gender(prof.get("name") or "", location, bio)
        if gender not in genders:
            return None, "off-gender"

    return {
        "login": login,
        "user_id": prof.get("id"),
        "url": prof.get("html_url") or f"https://github.com/{login}",
        "full_name": prof.get("name") or None,
        "company": (prof.get("company") or "").lstrip("@") or None,
        "location": location,
        "region": country["region"],
        "country": country["name"],
        "country_code": country["code"],
        "gender": gender or None,     # only set when a --gender filter ran
        "bio": bio or None,
        "site_url": site or None,
        "twitter": prof.get("twitter_username") or None,
        "hireable": bool(prof.get("hireable")),
        "followers": prof.get("followers") or 0,
        "public_repos": prof.get("public_repos") or 0,
        "created_at": prof.get("created_at") or "",       # the day they joined
        # Reuse the date fetched for the activity filter when it ran.
        "last_activity": (last_act if last_act is not None
                          else last_activity(login, repos)),
        # "" rather than absent when there is no address -- the record shape
        # stays the same whether the contact was reached by mail or by phone.
        "email": emails[0] if emails else "",
        "emails": emails,
        "email_sources": {k: v for k, v in sources.items() if v},
        # Phone / WhatsApp numbers found on the pages this scrape already
        # fetched. Kept on the record so the store sees them exactly as the
        # separate phone pass would have written them.
        "phones": phones,
    }, ""


# ---- main -----------------------------------------------------------------
def _write_cursor(path: str | None, term: str) -> None:
    """Record the location term in flight, so the next run resumes at it."""
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(term)
    except OSError:
        pass


# Which run a scrape is, and who has already been seen, now come from the
# database -- see RecordStore.next_run() / .done_keys().


def _walk_plan(terms) -> list[tuple[str, str, dict]]:
    """Every (follower shard, location, country) query, in the order to ask them.

    The shard is the OUTER loop, which is the whole point: it sweeps every
    country at `followers:>=500`, then every country at `100..499`, and so on.
    Walk it the other way round -- one location exhausted before the next -- and
    "United States" alone is six shards of up to 1000 users, so the run spends
    its first several hours in one country and a run that stops early (they all
    stop early, against these rate limits) has seen nothing else.

    Sweeping by shard instead means the walk is broad from the first hour, and
    it is the most-followed people in each country who are seen first, which is
    the half of any country worth having. Combined with `search_terms`, which
    interleaves the locations by country, the coverage stays even as it deepens.
    """
    return [(bucket, term, country)
            for bucket in FOLLOWER_BUCKETS
            for term, country in terms]


def _weighted_shuffle(plan, rng) -> list:
    """Shuffle the plan, but bias each query earlier the heavier its region.

    Efraimidis-Spirakis weighted ordering: give each item the key
    random()**(1/weight) and sort high-to-low. A heavier region draws keys
    nearer 1, so its queries *tend* to lead -- but every query keeps a random
    key, so the order is still random and lighter regions stay interleaved
    throughout, not exiled to the tail. With equal weights this is a plain
    shuffle. `REGION_WEIGHTS` sets the tilt; Europe's non-zero weight is what
    keeps it in the mix rather than dropped.
    """
    def key(query):
        weight = REGION_WEIGHTS.get(query[2]["region"], DEFAULT_REGION_WEIGHT)
        # weight <= 0 would divide by zero / drop the item; clamp to a floor so a
        # misconfigured weight quietly de-prioritises rather than crashes.
        return rng.random() ** (1.0 / max(weight, 1e-6))

    return sorted(plan, key=key, reverse=True)


def _resume_at(plan, cursor: str) -> tuple[list, int]:
    """Where the last run stopped: (plan from that query on, page to start at).

    The cursor is "shard|location|page" ("followers:0..2|Berlin|3"), naming the
    exact query *and depth* in flight. The page matters: without it a resumed run
    would restart every query at page 1 and re-walk ground it already covered.
    Shorter forms are still accepted -- "shard|location", or a bare location as
    someone would type it by hand -- and start that query at its first page.
    """
    key = (cursor or "").strip()
    if not key:
        return plan, 1
    parts = key.split("|")
    page = 1
    if len(parts) == 3 and parts[2].strip().isdigit():
        page = max(1, int(parts[2]))
        parts = parts[:2]
    want = "|".join(parts).lower()
    idx = next((i for i, (bucket, term, _) in enumerate(plan)
                if f"{bucket}|{term}".lower() == want or term.lower() == want), None)
    if idx is None:                     # a cursor we cannot place: start over
        return plan, 1
    return plan[idx:], page


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scrape GitHub users + their portfolio sites for emails.")
    ap.add_argument("--limit", type=int, default=0,
                    help="max users to keep THIS RUN (0 = no limit, the default)")
    ap.add_argument("--target", type=int, default=0,
                    help="keep sweeping until the output holds this many users "
                         "in total, counting the ones already in it (0 = off)")
    ap.add_argument("--pages", type=int, default=2,
                    help="search pages (100 users each) per query per sweep -- "
                         "small keeps the walk broad; 0 = drain each query's "
                         "full 1000 results before moving on (default: 2)")
    ap.add_argument("--regions", default=",".join(ALL_REGIONS),
                    help=f"comma-separated subset of {','.join(ALL_REGIONS)}")
    ap.add_argument("--countries", default=None,
                    help="keep only these countries (names or ISO codes, "
                         "comma-separated) -- a finer net than --regions, e.g. "
                         "'Brazil,Argentina' or 'BR,CA'")
    ap.add_argument("--age-min", type=float, default=None,
                    help="keep only accounts at least this many years old")
    ap.add_argument("--age-max", type=float, default=None,
                    help="keep only accounts under this many years old")
    ap.add_argument("--gender", default=None,
                    help="keep only this gender ('male' or 'female'), inferred "
                         "by Claude for near-keepers -- needs ANTHROPIC_API_KEY")
    ap.add_argument("--providers", default=None,
                    help="keep only users whose best email is in these mailbox "
                         "buckets (comma-separated: "
                         + ", ".join(k for k, _ in EMAIL_PROVIDERS) + ")")
    ap.add_argument("--joined-after", default=None, metavar="DATE",
                    help="keep only accounts created on/after this ISO date "
                         "(e.g. 2024-01-01)")
    ap.add_argument("--joined-before", default=None, metavar="DATE",
                    help="keep only accounts created before this ISO date")
    ap.add_argument("--active-after", default=None, metavar="DATE",
                    help="keep only users last publicly active on/after this "
                         "ISO date (costs one extra API call per candidate)")
    ap.add_argument("--active-before", default=None, metavar="DATE",
                    help="keep only users last publicly active before this "
                         "ISO date")
    # Users go straight into the database (the `records` table), which is also
    # where the resume set comes from -- there is no output file to name.
    ap.add_argument("--skipped", default="1",
                    help="remember users ruled out (no email, off-region, an "
                         "org) in the `skipped` table so later runs do not "
                         "re-fetch them. '' to not remember them")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between users (default: 0.5)")
    ap.add_argument("--search-delay", type=float, default=2.2,
                    help="seconds between search calls -- the search API allows "
                         "30/min authenticated (default: 2.2)")
    ap.add_argument("--shuffle", action="store_true",
                    help="walk the queries in a random order, so each run "
                         "explores a different slice of the world and restarts "
                         "stop re-covering the same first queries")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed for --shuffle, to reproduce a run's order "
                         "(default: a fresh random order each run)")
    ap.add_argument("--start-location", default=None,
                    help="resume at this query -- 'followers:0..2|Berlin' as "
                         "written to --cursor-out, or a bare location term. "
                         "Ignored when --shuffle is on")
    ap.add_argument("--users", default=None,
                    help="comma-separated logins to scrape instead of searching")
    ap.add_argument("--cursor-out", default=None,
                    help="write the in-flight location term here (resume cursor)")
    ap.add_argument("--no-commits", action="store_true",
                    help="skip public-push-event commit emails")
    ap.add_argument("--no-site", action="store_true",
                    help="skip the portfolio-site crawl")
    ap.add_argument("--no-readme", action="store_true",
                    help="skip the profile README")
    args = ap.parse_args()

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    bad = [r for r in regions if r not in REGIONS]
    if bad:
        print(f"unknown region(s): {', '.join(bad)} "
              f"(pick from {', '.join(ALL_REGIONS)})", file=sys.stderr)
        return 2

    # Country / age / gender / provider narrowing (all optional, default off).
    countries = {c.strip().lower() for c in (args.countries or "").split(",")
                 if c.strip()} or None
    genders = {g.strip().lower() for g in (args.gender or "").split(",")
               if g.strip() in ("male", "female")} or None
    provider_keys = {k for k, _ in EMAIL_PROVIDERS}
    providers = {p.strip().lower() for p in (args.providers or "").split(",")
                 if p.strip().lower() in provider_keys} or None
    unknown = {p.strip().lower() for p in (args.providers or "").split(",")
               if p.strip()} - provider_keys
    if unknown:
        print(f"unknown mailbox bucket(s): {', '.join(sorted(unknown))} "
              f"(pick from {', '.join(sorted(provider_keys))})", file=sys.stderr)
        return 2
    if genders and not ANTHROPIC_API_KEY:
        print("! --gender needs ANTHROPIC_API_KEY (Claude infers gender); "
              "ignoring the gender filter.", file=sys.stderr)
        genders = None

    # Joined / last-active calendar windows. A typo'd date should stop the run
    # loudly, not silently scrape the whole world.
    date_bounds = {}
    for flag in ("joined_after", "joined_before",
                 "active_after", "active_before"):
        raw = getattr(args, flag)
        ts = date_ts(raw) if raw else None
        if raw and ts is None:
            print(f"unparseable --{flag.replace('_', '-')} date: {raw!r} "
                  f"(use ISO, e.g. 2024-01-01)", file=sys.stderr)
            return 2
        date_bounds[flag] = ts

    if not TOKEN:
        print("! No GITHUB_TOKEN set: search is 10 req/min and the API 60 req/hr.\n"
              "  Create one at https://github.com/settings/tokens (no scopes needed)\n"
              "  and put GITHUB_TOKEN=... in .env. Continuing anyway, slowly.",
              file=sys.stderr)

    print(f"Regions: {', '.join(REGIONS[r] for r in regions)}", file=sys.stderr)
    narrowing = []
    if countries:
        narrowing.append(f"countries={','.join(sorted(countries))}")
    if args.age_min is not None:
        narrowing.append(f"age>={args.age_min:g}")
    if args.age_max is not None:
        narrowing.append(f"age<{args.age_max:g}")
    if args.joined_after:
        narrowing.append(f"joined>={args.joined_after}")
    if args.joined_before:
        narrowing.append(f"joined<{args.joined_before}")
    if args.active_after:
        narrowing.append(f"active>={args.active_after}")
    if args.active_before:
        narrowing.append(f"active<{args.active_before}")
    if providers:
        narrowing.append(f"email={','.join(sorted(providers))}")
    if genders:
        narrowing.append(f"gender={','.join(sorted(genders))}")
    if narrowing:
        print(f"Filters: {'; '.join(narrowing)}", file=sys.stderr)
    print("Only users with a discoverable email or phone number are kept.",
          file=sys.stderr)

    store = RecordStore("github")
    done = store.done_keys()
    already = len(done)                 # users the archive already holds
    if done:
        print(f"Resuming: {already} users already stored.", file=sys.stderr)

    # Everyone we have already looked at and turned down. Without this the next
    # run re-fetches every one of them -- 3-5 API calls each, and most people
    # scanned are turned down -- to reach the same verdict. Remembering the
    # verdict is what makes a second run cheap. Clear the `skipped` table to
    # re-examine them (worth doing after widening the regions, or to recheck
    # people who had no public email at the time).
    skips = SkipStore("github") if args.skipped else None
    skipped = skips.keys() if skips else set()
    if skipped:
        print(f"Skipping: {len(skipped)} users already ruled out.",
              file=sys.stderr)
    done |= skipped
    if args.target and already >= args.target:
        print(f"Target of {args.target} users already met -- nothing to do.",
              file=sys.stderr)
        return 0

    run_no = store.next_run()
    print(f"This is run #{run_no} -- users it finds are tagged with it.",
          file=sys.stderr)

    kept = scanned = ruled_out = 0
    stop = False
    with store as out:

        def keep(login: str) -> bool:
            """Scrape one login and append it if it survives the filters."""
            nonlocal kept, scanned, ruled_out
            try:
                rec, why = scrape_user(
                    login, regions=regions, site_delay=min(args.delay, 1.0),
                    use_commits=not args.no_commits, use_site=not args.no_site,
                    use_readme=not args.no_readme, countries=countries,
                    age_min=args.age_min, age_max=args.age_max, genders=genders,
                    providers=providers, **date_bounds,
                )
            except RateLimited as e:
                done.add(login)
                print(f"  ! {e}", file=sys.stderr)
                return False
            except Exception as e:  # noqa: BLE001 -- one bad profile is not fatal
                # Remembered for THIS run only -- never skip-listed. A timeout or
                # a 500 is a fact about the network, not about the person, so a
                # later run must be free to ask again; but one person surfaces in
                # several queries (their country, their city, each shard), and
                # without this they would be retried on every one of them.
                done.add(login)
                print(f"  ! {login}: {e}", file=sys.stderr)
                time.sleep(1.5)
                return False
            finally:
                scanned += 1
            done.add(login)
            if not rec:
                # Skip-list only *filter-independent* verdicts. "off-country",
                # "off-age", "off-provider" and "off-gender" are relative to THIS
                # run's flags -- a later run with different flags would want
                # these people, so
                # persisting them would wrongly hide them. Those just skip for
                # the run (they're in `done` above). "not-a-user"/"no-contact"/
                # "off-region" are stable enough to remember across runs.
                ruled_out += 1
                if skips and why in PERSISTED_SKIP_REASONS:
                    skips.add(login, why)
                return False
            rec["run"] = run_no      # which scrape this user came in on
            out.add(rec)
            kept += 1
            got = "+".join(rec["email_sources"]) or "phone"
            # A user kept for a number alone has no address to show, so the
            # line names whichever contact they were actually kept for.
            reach = rec["email"] or (rec["phones"][0]["number"] if rec["phones"]
                                     else "")
            print(f"  [{kept}/{scanned} scanned] + {login}: "
                  f"{rec['full_name'] or ''} <{reach}> "
                  f"| {rec['country']} | via {got}", file=sys.stderr)
            return True

        if args.users:
            for login in (u.strip() for u in args.users.split(",") if u.strip()):
                keep(login)
                time.sleep(args.delay)
        else:
            full_plan = _walk_plan(search_terms(regions))
            plan, page = full_plan, 1
            if args.shuffle:
                # A fresh random order each run, so a restart heads into a
                # different part of the world instead of re-walking the same
                # first queries (whose users are already on disk). The seed is
                # printed so a run can be reproduced with --seed if needed. The
                # resume cursor does not apply -- position means nothing once
                # the order is random -- so --start-location is ignored.
                seed = args.seed if args.seed is not None else random.randrange(1 << 30)
                full_plan = _weighted_shuffle(full_plan, random.Random(seed))
                plan = full_plan
                print(f"  shuffled walk order (seed {seed}) -- exploring at "
                      f"random, weighted toward the Americas", file=sys.stderr)
                if args.start_location:
                    print("  (ignoring --start-location: --shuffle is on)",
                          file=sys.stderr)
            elif args.start_location:
                plan, page = _resume_at(full_plan, args.start_location)
            # 0 = no cap: take a query's whole 1000 results before moving on.
            per_sweep = args.pages if args.pages > 0 else SEARCH_PAGE_CAP
            print(f"  {len(full_plan)} (location x follower shard) queries, "
                  f"{per_sweep} page(s) of 100 per query per sweep",
                  file=sys.stderr)

            # Sweep the whole plan shallowly, then sweep it again deeper, and
            # again -- rather than draining each query to its 1000-result floor
            # before the next one is touched. Breadth first: after one sweep
            # every country has been looked at; depth is what waits.
            retired: set[str] = set()   # queries GitHub has no more results for
            while not stop and page <= SEARCH_PAGE_CAP:
                pages = range(page, min(page + per_sweep, SEARCH_PAGE_CAP + 1))
                print(f"\n### sweep: pages {pages.start}-{pages.stop - 1} of "
                      f"{len(plan) - len(retired)} queries", file=sys.stderr)
                swept = 0
                for bucket, term, country in plan:
                    if stop:
                        break
                    key = f"{bucket}|{term}"
                    if key in retired:  # exhausted on an earlier sweep -- skip
                        continue
                    # Cursor is written at the START of a query, with the depth:
                    # a run stopped mid-query resumes at that same query and
                    # page, and logins already saved are skipped -- so nothing is
                    # re-fetched and nothing is missed.
                    _write_cursor(args.cursor_out, f"{key}|{pages.start}")
                    print(f"\n== {term} ({country['name']}) {bucket} "
                          f"p{pages.start} ==", file=sys.stderr)
                    logins, more = search_logins(term, bucket, pages,
                                                 args.search_delay)
                    if not more:
                        retired.add(key)
                    swept += 1
                    for login in logins:
                        if args.limit and kept >= args.limit:
                            stop = True
                            break
                        if args.target and already + kept >= args.target:
                            stop = True
                            break
                        if login in done:
                            continue
                        keep(login)
                        time.sleep(args.delay)
                    time.sleep(args.search_delay)
                # A resumed run starts mid-plan; from the next sweep on, walk it
                # whole -- the queries it skipped still owe us their first pages.
                plan, page = full_plan, pages.stop
                if swept == 0 or len(retired) >= len(full_plan):
                    break               # every query is exhausted: the walk is over

    if skips:
        skips.close()

    print(f"\nDone. Kept {kept} users with emails ({scanned} scanned); "
          f"{store.count()} stored in total.", file=sys.stderr)
    if skips and ruled_out > 0:
        print(f"      {ruled_out} ruled out and remembered "
              f"(a later run will not re-fetch them)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
