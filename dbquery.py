#!/usr/bin/env python3
"""The read side: list, detail, export, stats and facets, as SQL.

Every filter the UI offers is a WHERE clause here, every sort an ORDER BY, and
every page a LIMIT -- so the work MySQL does is proportional to the page being
returned rather than to the size of the archive. That is the whole reason the
data moved out of JSON files.

Two rules this module keeps to, because the UI depends on them:

  * **Filter order matters.** The sent/unsent counts are taken over the
    source+search set, and the facet counts over source+search+sent, *before*
    the country/gender/age filters narrow anything. A facet count means "how
    many you would get if you picked this", which only holds if picking one
    does not change the others.
  * **Sorting puts blanks last**, in both directions. A plain ORDER BY floats
    empty names to the top of a Z-A sort; the explicit ``(col = '')`` term is
    what keeps them at the bottom either way.
"""
import json
from datetime import datetime, timezone

import db
import records as rec_mod
from common import EMAIL_PROVIDERS

MAX_PER_PAGE = 100
DEFAULT_SORT = "newest"

# Fields the API hands to the web app. Unchanged from the pre-database server,
# so the frontend did not need to know any of this happened.
PUBLIC_FIELDS = (
    "id", "source", "emails", "name", "username", "title", "url", "site_url",
    "created_at", "activity_at", "run", "preview", "tags", "location",
    "organization", "apply_links", "messaging", "links", "posts", "post_count",
    "country", "country_code", "gender",
    "phones", "phone", "phone_count", "has_whatsapp",
    "messaged", "messaged_count", "messaged_at", "messaged_to", "messaged_manual",
)

# sort key -> ORDER BY. The date sorts keep their old names so links and
# bookmarks made before the other columns existed still work. `id` breaks ties
# so a row cannot appear on two pages (or on neither) as you page through.
SORTS = {
    "newest":       "c.ts DESC, c.id ASC",
    "oldest":       "c.ts ASC, c.id ASC",
    # Within one run the newest contact leads, so a run reads like a batch.
    "run_desc":     "c.run DESC, c.ts DESC, c.id ASC",
    "run_asc":      "c.run ASC, c.ts DESC, c.id ASC",
    "name_asc":     "(c.name_key = '') ASC, c.name_key ASC, c.id ASC",
    "name_desc":    "(c.name_key <> '') DESC, c.name_key DESC, c.id ASC",
    "email_asc":    "(c.email_key = '') ASC, c.email_key ASC, c.id ASC",
    "email_desc":   "(c.email_key <> '') DESC, c.email_key DESC, c.id ASC",
    "country_asc":  "(c.country_key = '') ASC, c.country_key ASC, c.id ASC",
    "country_desc": "(c.country_key <> '') DESC, c.country_key DESC, c.id ASC",
}

_SECONDS_PER_YEAR = 365.25 * 86400


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value):
    """Parse a float, or None for blank/garbage. Negatives clamp to 0."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _jload(value, fallback=None):
    try:
        out = json.loads(value or "null")
    except (ValueError, TypeError):
        return [] if fallback is None else fallback
    return out if out is not None else ([] if fallback is None else fallback)


# ---- WHERE builders -------------------------------------------------------
class Where:
    """A growing list of AND-ed conditions and their parameters."""

    def __init__(self):
        self.parts: list[str] = []
        self.params: list = []

    def add(self, sql: str, *params) -> None:
        self.parts.append(sql)
        self.params.extend(params)

    def clone(self) -> "Where":
        other = Where()
        other.parts = list(self.parts)
        other.params = list(self.params)
        return other

    @property
    def sql(self) -> str:
        return " AND ".join(self.parts) if self.parts else "1 = 1"


def _base_where(source: str) -> Where:
    w = Where()
    if source != "all":
        w.add("c.source = %s", source)
    else:
        # "All" means the sources the UI lists, not everything in the table --
        # dev.to is stored but not one of the tabs, and must not appear here.
        keys = rec_mod.LISTED_KEYS
        marks = ", ".join(["%s"] * len(keys))
        w.add(f"c.source IN ({marks})", *keys)
    return w


def _add_search(w: Where, q: str) -> None:
    """Every whitespace-separated term must appear somewhere in the record.

    Substring matching, not word matching -- typing "three" has always found
    "three.js", and a FULLTEXT index would quietly stop doing that.
    """
    for term in (q or "").strip().lower().split():
        w.add("c.search_blob LIKE %s", f"%{term}%")


def _add_sent(w: Where, selected: list[str]) -> None:
    if selected == ["sent"]:
        w.add("c.messaged_count > 0")
    elif selected == ["unsent"]:
        w.add("c.messaged_count = 0")


def _add_country(w: Where, wanted: set[str]) -> None:
    if not wanted:
        return
    names = sorted(wanted)
    marks = ", ".join(["%s"] * len(names))
    # Names OR ISO codes, case-insensitive -- the UI sends whichever the pill
    # carried, and a URL may hold either.
    w.add(f"(LOWER(c.country) IN ({marks}) OR LOWER(c.country_code) IN ({marks}))",
          *names, *names)


def _add_gender(w: Where, wanted: set[str]) -> None:
    if not wanted:
        return
    marks = ", ".join(["%s"] * len(wanted))
    # An unset gender reads as "unknown", so that pill has to match '' too.
    w.add(f"(CASE WHEN c.gender = '' THEN 'unknown' ELSE LOWER(c.gender) END) "
          f"IN ({marks})", *sorted(wanted))


def _add_contactable(w: Where, wanted: set) -> None:
    """Narrow to contacts reachable by phone / WhatsApp.

    Any-of, like the other pills: picking both means "has either", and picking
    neither means no filtering. "whatsapp" is the stricter of the two -- it
    means the number was published as a WhatsApp contact, not merely that one
    exists.
    """
    if not wanted:
        return
    if wanted == {"whatsapp"}:
        w.add("c.has_whatsapp = 1")
    else:
        # {"phone"} or both: any number at all.
        w.add("c.phone_count > 0")


def _add_runs(w: Where, wanted: set[int]) -> None:
    if not wanted:
        return
    marks = ", ".join(["%s"] * len(wanted))
    w.add(f"c.run IN ({marks})", *sorted(wanted))


def _add_date(w: Where, column: str, op: str, date: str) -> None:
    """After/before a calendar day: after inclusive, before exclusive.

    A record missing the date in question cannot satisfy a condition on it, so
    it drops out -- hence the `> 0` on the before branch.
    """
    cutoff = rec_mod.parse_ts(date) if date else 0.0
    if op not in ("after", "before") or not cutoff:
        return
    if op == "after":
        w.add(f"{column} >= %s", cutoff)
    else:
        w.add(f"{column} > 0 AND {column} < %s", cutoff)


def _add_age(w: Where, lo, hi, now_ts: float) -> None:
    """Account age in years, min inclusive and max EXCLUSIVE.

    A one-year band [N, N+1) means "N years old" and the bands tile without
    overlap, which is what makes them agree with the facet counts. Expressed
    against the join timestamp so an index can be used: older than N years
    means joined before now - N years.
    """
    if lo is None and hi is None:
        return
    w.add("c.created_ts > 0")
    if lo is not None:
        w.add("c.created_ts <= %s", now_ts - lo * _SECONDS_PER_YEAR)
    if hi is not None:
        w.add("c.created_ts > %s", now_ts - hi * _SECONDS_PER_YEAR)


# ---- row -> API payload ---------------------------------------------------
def contact_payload(row: dict) -> dict:
    """One contacts row in the exact shape the web app already expects."""
    return {
        "id": row["id"],
        "source": row["source"],
        "emails": _jload(row.get("emails")),
        "name": row.get("name") or "",
        "username": row.get("username") or "",
        "title": row.get("title") or "",
        "url": row.get("url") or "",
        "site_url": row.get("site_url") or "",
        "created_at": row.get("created_at") or "",
        "activity_at": row.get("activity_at") or "",
        "run": row.get("run") or 0,
        "preview": row.get("preview") or "",
        "tags": _jload(row.get("tags")),
        "location": row.get("location") or "",
        "organization": row.get("organization") or "",
        "apply_links": _jload(row.get("apply_links")),
        "messaging": _jload(row.get("messaging")),
        "links": _jload(row.get("links")),
        "posts": _jload(row.get("posts")),
        "post_count": row.get("post_count") or 1,
        "country": row.get("country") or "",
        "country_code": row.get("country_code") or "",
        "gender": row.get("gender") or "",
        # Numbers best-first: a WhatsApp number leads, because it is a channel
        # the person published to be contacted on.
        "phones": _jload(row.get("phones")),
        "phone": row.get("primary_phone") or "",
        "phone_count": row.get("phone_count") or 0,
        "has_whatsapp": bool(row.get("has_whatsapp")),
        "messaged": bool(row.get("messaged_count") or 0),
        "messaged_count": row.get("messaged_count") or 0,
        "messaged_at": row.get("messaged_at") or "",
        "messaged_to": row.get("messaged_to") or "",
        "messaged_manual": bool(row.get("messaged_manual")),
    }


# ---- stats + source list --------------------------------------------------
def _stats_for(source: str, noun: str) -> dict:
    w = _base_where(source)
    row = db.query_one(
        f"SELECT COALESCE(SUM(c.post_count), 0) AS total_posts, "
        f"       COALESCE(SUM(c.email_count), 0) AS total_emails, "
        f"       MIN(NULLIF(c.ts, 0)) AS earliest, MAX(NULLIF(c.ts, 0)) AS latest "
        f"FROM contacts c WHERE {w.sql}", w.params) or {}
    unique = db.scalar(
        f"SELECT COUNT(DISTINCT ce.email) AS n FROM contact_emails ce "
        f"JOIN contacts c ON c.id = ce.contact_id WHERE {w.sql}",
        w.params, default=0)
    earliest, latest = row.get("earliest"), row.get("latest")
    return {
        "total_posts": int(row.get("total_posts") or 0),
        "total_emails": int(row.get("total_emails") or 0),
        "unique_emails": int(unique or 0),
        "earliest": rec_mod.fmt_date(float(earliest)) if earliest else None,
        "latest": rec_mod.fmt_date(float(latest)) if latest else None,
        "noun": noun,
    }


# Stats, facets and totals all aggregate the whole matching set, so they cost
# the same whether you are on page 1 or page 40 -- and paging does not change
# them. They are cached against the filters that produced them and dropped
# wholesale whenever anything writes, which turns page-flipping into just the
# indexed LIMIT query.
_STATS_CACHE: dict = {}
_SOURCES_CACHE: list = []
_AGG_CACHE: dict = {}
_AGG_MAX = 256


def invalidate_caches() -> None:
    _STATS_CACHE.clear()
    _SOURCES_CACHE.clear()
    _AGG_CACHE.clear()


_MISS = object()


def _memo(key, compute):
    """Cache one aggregate result under `key`, bounded so it cannot grow forever.

    Read with a single ``get`` rather than "in, then index": the HTTP server is
    threaded and the enrichment workers call ``invalidate_caches`` whenever an
    inference lands, so a hit tested in one statement can be gone by the next.
    A racing miss just recomputes, which is only ever a wasted query.
    """
    value = _AGG_CACHE.get(key, _MISS)
    if value is not _MISS:
        return value
    value = compute()
    if len(_AGG_CACHE) >= _AGG_MAX:
        _AGG_CACHE.clear()  # cheap eviction; these are all equally cold
    _AGG_CACHE[key] = value
    return value


def stats_for(source: str) -> dict:
    if source not in rec_mod.NOUNS:
        source = "all"
    cached = _STATS_CACHE.get(source, _MISS)
    if cached is not _MISS:
        return cached
    value = _stats_for(source, rec_mod.NOUNS[source])
    _STATS_CACHE[source] = value
    return value


def source_list() -> list[dict]:
    # Snapshot the list before testing it: another thread clearing the cache
    # between the test and the read would otherwise hand back an empty list.
    cached = list(_SOURCES_CACHE)
    if cached:
        return cached
    counts = {r["source"]: int(r["n"]) for r in db.query(
        "SELECT source, COUNT(*) AS n FROM contacts GROUP BY source")}
    # Sum the listed sources only -- a stored-but-unlisted source (dev.to) is
    # not in any tab, so counting it would make "All" disagree with itself.
    out = [{"key": "all", "label": "All", "noun": rec_mod.NOUNS["all"],
            "count": sum(counts.get(k, 0) for k in rec_mod.LISTED_KEYS)}]
    for s in rec_mod.SOURCES:
        out.append({"key": s["key"], "label": s["label"], "noun": s["noun"],
                    "count": counts.get(s["key"], 0)})
    _SOURCES_CACHE[:] = out
    return out


# ---- facets ---------------------------------------------------------------
def _facets(w: Where, now_ts: float) -> dict:
    """Counts per country, gender, age band and run over the given set."""
    countries = [
        {"name": r["name"], "code": r["code"] or "", "count": int(r["n"])}
        for r in db.query(
            f"SELECT c.country AS name, MAX(c.country_code) AS code, "
            f"       COUNT(*) AS n FROM contacts c "
            f"WHERE {w.sql} AND c.country <> '' AND LOWER(c.country) <> 'unknown' "
            f"GROUP BY c.country ORDER BY n DESC, LOWER(c.country) ASC",
            w.params)
    ]

    # Genders and age bands in one pass -- same rows, different buckets.
    y = _SECONDS_PER_YEAR
    row = db.query_one(
        f"SELECT "
        f"  SUM(LOWER(c.gender) = 'male') AS male, "
        f"  SUM(LOWER(c.gender) = 'female') AS female, "
        f"  SUM(c.gender = '' OR LOWER(c.gender) NOT IN ('male','female')) AS unknown, "
        f"  SUM(c.phone_count > 0) AS with_phone, "
        f"  SUM(c.has_whatsapp = 1) AS with_whatsapp, "
        f"  SUM(c.created_ts <= 0) AS a_unknown, "
        f"  SUM(c.created_ts > 0 AND c.created_ts >  %s) AS a_lt1, "
        f"  SUM(c.created_ts > 0 AND c.created_ts <= %s AND c.created_ts > %s) AS a_1to3, "
        f"  SUM(c.created_ts > 0 AND c.created_ts <= %s AND c.created_ts > %s) AS a_3to5, "
        f"  SUM(c.created_ts > 0 AND c.created_ts <= %s AND c.created_ts > %s) AS a_5to10, "
        f"  SUM(c.created_ts > 0 AND c.created_ts <= %s) AS a_gte10 "
        f"FROM contacts c WHERE {w.sql}",
        [now_ts - 1 * y,
         now_ts - 1 * y, now_ts - 3 * y,
         now_ts - 3 * y, now_ts - 5 * y,
         now_ts - 5 * y, now_ts - 10 * y,
         now_ts - 10 * y] + w.params) or {}

    def n(key):
        return int(row.get(key) or 0)

    genders = {"male": n("male"), "female": n("female"), "unknown": n("unknown")}
    contactable = {"phone": n("with_phone"), "whatsapp": n("with_whatsapp")}
    ages = {"lt1": n("a_lt1"), "1to3": n("a_1to3"), "3to5": n("a_3to5"),
            "5to10": n("a_5to10"), "gte10": n("a_gte10"),
            "unknown": n("a_unknown")}

    # Run 0 means "the source does not number its runs", which is not a step
    # anyone can meaningfully pick.
    runs = [{"run": int(r["run"]), "count": int(r["n"])} for r in db.query(
        f"SELECT c.run AS run, COUNT(*) AS n FROM contacts c "
        f"WHERE {w.sql} AND c.run > 0 GROUP BY c.run ORDER BY c.run ASC",
        w.params)]

    return {"countries": countries, "genders": genders, "ages": ages,
            "runs": runs, "contactable": contactable}


# ---- the list -------------------------------------------------------------
def query_records(source: str, q: str, sort: str, page: int, per_page: int,
                  messaged: str = "all", country: str = "", gender: str = "",
                  age_min: str = "", age_max: str = "", runs: str = "",
                  joined_op: str = "", joined_date: str = "",
                  active_op: str = "", active_date: str = "",
                  contactable: str = "", on_page=None):
    if source != "all" and source not in rec_mod.SOURCE_BY_KEY:
        source = "all"  # unknown source -> behave like "all"

    # Which scrape runs actually exist in this source, judged over the whole
    # set (before search narrows it). Used to drop a step filter that points at
    # a run which no longer exists -- e.g. one merged away, leaving a stale
    # `runs=` in the URL -- so a phantom step falls back to "all" instead of
    # hiding every contact the user never knowingly filtered out.
    base = _base_where(source)
    source_runs = _memo(("runs", source), lambda: {int(r["run"]) for r in db.query(
        f"SELECT DISTINCT c.run AS run FROM contacts c WHERE {base.sql}",
        base.params)})

    q = (q or "").strip().lower()
    searched = base.clone()
    _add_search(searched, q)

    # Counts for the sent/unsent filter, over the current source+search set.
    def _sent_counts():
        row = db.query_one(
            f"SELECT COUNT(*) AS total, SUM(c.messaged_count > 0) AS sent "
            f"FROM contacts c WHERE {searched.sql}", searched.params) or {}
        all_n = int(row.get("total") or 0)
        sent_n = int(row.get("sent") or 0)
        return {"all": all_n, "sent": sent_n, "unsent": all_n - sent_n}

    messaged_counts = _memo(("sent", source, q), _sent_counts)

    # `messaged` is a set of {sent, unsent} (comma-separated). Selecting both
    # (or neither) is the same as "all" -- no filtering.
    sel = sorted({m for m in (messaged or "").split(",")
                  if m in ("sent", "unsent")})
    filtered = searched.clone()
    _add_sent(filtered, sel)
    messaged = ",".join(sel) if sel else "all"

    # Facets describe this set (before country/gender/age narrow it). The age
    # bands are cached against the hour, not the second -- a band boundary that
    # moves by minutes cannot change which band an account falls in.
    now_ts = datetime.now(timezone.utc).timestamp()
    facets = _memo(("facets", source, q, messaged, int(now_ts // 3600)),
                   lambda: _facets(filtered, now_ts))

    wanted_countries = {c.strip().lower() for c in (country or "").split(",")
                        if c.strip()}
    _add_country(filtered, wanted_countries)

    wanted_genders = {g.strip().lower() for g in (gender or "").split(",")
                      if g.strip() in ("male", "female", "unknown")}
    _add_gender(filtered, wanted_genders)

    wanted_runs = {_to_int(x.strip(), -1) for x in (runs or "").split(",")
                   if x.strip()} & source_runs
    _add_runs(filtered, wanted_runs)

    wanted_contactable = {c.strip().lower() for c in (contactable or "").split(",")
                          if c.strip().lower() in ("phone", "whatsapp")}
    _add_contactable(filtered, wanted_contactable)

    _add_date(filtered, "c.created_ts", joined_op, joined_date)
    _add_date(filtered, "c.activity_ts", active_op, active_date)

    lo, hi = _to_float(age_min), _to_float(age_max)
    age_min = "" if lo is None else str(lo)
    age_max = "" if hi is None else str(hi)
    _add_age(filtered, lo, hi, now_ts)

    # The total drives the pager, so it depends on every filter -- but not on
    # which page is being asked for, which is what makes it worth caching.
    total = _memo(("total", filtered.sql, tuple(filtered.params)),
                  lambda: int(db.scalar(f"SELECT COUNT(*) AS n FROM contacts c "
                                        f"WHERE {filtered.sql}",
                                        filtered.params, default=0)))

    if sort not in SORTS:
        sort = DEFAULT_SORT
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = db.query(
        f"SELECT * FROM contacts c WHERE {filtered.sql} "
        f"ORDER BY {SORTS[sort]} LIMIT %s OFFSET %s",
        filtered.params + [per_page, offset])
    items = [contact_payload(r) for r in rows]
    if on_page:
        on_page(rows)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "source": source,
        "messaged": messaged,
        "messaged_counts": messaged_counts,
        "country": ",".join(sorted(wanted_countries)),
        "gender": ",".join(sorted(wanted_genders)),
        "runs": ",".join(str(r) for r in sorted(wanted_runs)),
        "contactable": ",".join(sorted(wanted_contactable)),
        "age_min": age_min,
        "age_max": age_max,
        "facets": facets,
        "stats": stats_for(source),
        "sources": source_list(),
    }


# ---- one contact ----------------------------------------------------------
def detail_record(rec_id: str) -> dict | None:
    """Full view of one merged contact: public fields + every post's full text."""
    row = db.query_one("SELECT * FROM contacts c WHERE c.id = %s", (rec_id,))
    if row is None:
        return None
    view = contact_payload(row)
    # Every occurrence with its FULL text -- served only by this endpoint, in
    # the same newest-first order the merge used to build the teaser list.
    view["posts_full"] = [
        {"title": r["title"] or "", "url": r["url"] or "",
         "created_at": r["created_at"] or "", "text": r["full_text"] or ""}
        for r in db.query(
            "SELECT title, url, created_at, full_text FROM records "
            "WHERE contact_id = %s ORDER BY ts DESC, id ASC", (rec_id,))
    ]
    return view


def contact_row(rec_id: str) -> dict | None:
    return db.query_one("SELECT * FROM contacts WHERE id = %s", (rec_id,))


def contact_emails(rec_id: str) -> list[str]:
    return [r["email"] for r in db.query(
        "SELECT email FROM contact_emails WHERE contact_id = %s ORDER BY pos",
        (rec_id,))]


# ---- the export dialog ----------------------------------------------------
def export_records(source: str, gender: str = "", active_op: str = "",
                   active_date: str = "", joined_op: str = "",
                   joined_date: str = "", runs: str = "", country: str = "",
                   messaged: str = "", provider: str = "",
                   contactable: str = "", limit: int = 0):
    """Rows for the CSV export dialog: filtered, sorted, capped.

    Also returns the option lists (scrape runs, countries, providers) the
    dialog builds its controls from -- computed over the WHOLE source set, so
    choices don't vanish from the dialog as the selection narrows. Unlike the
    list this is not paginated: the preview table IS the file that gets
    downloaded, so the response holds every row (bounded by ``limit``).
    """
    # A row is worth exporting when it carries some way to reach the person --
    # an address or a number. Requiring an address would drop the contacts that
    # published only a WhatsApp number.
    base = _base_where(source)
    base.add("(c.primary_email <> '' OR c.phone_count > 0)")

    options = {
        "runs": [{"run": int(r["run"]), "count": int(r["n"])} for r in db.query(
            f"SELECT c.run AS run, COUNT(*) AS n FROM contacts c "
            f"WHERE {base.sql} GROUP BY c.run ORDER BY c.run ASC", base.params)],
        "countries": [
            {"name": r["name"], "code": r["code"] or "", "count": int(r["n"])}
            for r in db.query(
                f"SELECT c.country AS name, MAX(c.country_code) AS code, "
                f"COUNT(*) AS n FROM contacts c WHERE {base.sql} AND c.country <> '' "
                f"GROUP BY c.country ORDER BY n DESC, LOWER(c.country) ASC",
                base.params)],
        # Fixed order, every bucket listed even at zero -- the dialog's
        # provider pills shouldn't reshuffle or disappear between sources.
        "providers": [],
    }
    provider_counts = {r["provider"]: int(r["n"]) for r in db.query(
        f"SELECT c.provider AS provider, COUNT(*) AS n FROM contacts c "
        f"WHERE {base.sql} GROUP BY c.provider", base.params)}
    options["providers"] = [{"key": k, "label": label,
                             "count": provider_counts.get(k, 0)}
                            for k, label in EMAIL_PROVIDERS]
    reach = db.query_one(
        f"SELECT SUM(c.phone_count > 0) AS with_phone, "
        f"       SUM(c.has_whatsapp = 1) AS with_whatsapp "
        f"FROM contacts c WHERE {base.sql}", base.params) or {}
    options["contactable"] = [
        {"key": "phone", "label": "Has phone",
         "count": int(reach.get("with_phone") or 0)},
        {"key": "whatsapp", "label": "On WhatsApp",
         "count": int(reach.get("with_whatsapp") or 0)},
    ]

    w = base.clone()
    # Sent/unsent: same contract as the list -- picking both (or neither)
    # means "all".
    _add_sent(w, sorted({m for m in (messaged or "").split(",")
                         if m in ("sent", "unsent")}))
    _add_gender(w, {g.strip().lower() for g in (gender or "").split(",")
                    if g.strip() in ("male", "female", "unknown")})

    # Mailbox provider of the main email -- the one address that gets exported.
    valid = {k for k, _ in EMAIL_PROVIDERS}
    wanted_providers = {p.strip().lower() for p in (provider or "").split(",")
                        if p.strip().lower() in valid}
    if wanted_providers:
        marks = ", ".join(["%s"] * len(wanted_providers))
        w.add(f"c.provider IN ({marks})", *sorted(wanted_providers))

    _add_country(w, {c.strip().lower() for c in (country or "").split(",")
                     if c.strip()})
    _add_date(w, "c.activity_ts", active_op, active_date)
    _add_date(w, "c.created_ts", joined_op, joined_date)

    wanted_runs = {_to_int(x.strip(), -1) for x in (runs or "").split(",")
                   if x.strip()}
    wanted_runs.discard(-1)
    _add_runs(w, wanted_runs)
    _add_contactable(w, {c.strip().lower() for c in (contactable or "").split(",")
                         if c.strip().lower() in ("phone", "whatsapp")})

    matched = int(db.scalar(f"SELECT COUNT(*) AS n FROM contacts c "
                            f"WHERE {w.sql}", w.params, default=0))

    sql = (f"SELECT c.id, c.name, c.username, c.primary_email, c.provider, "
           f"c.gender, c.country, c.run, c.created_at, c.activity_at, "
           f"c.primary_phone, c.has_whatsapp, c.phone_count, "
           f"c.messaged_count FROM contacts c WHERE {w.sql} "
           f"ORDER BY c.ts DESC, c.id ASC")
    params = list(w.params)
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)

    # Slim rows: exactly what the preview table and the CSV need. Only the main
    # email (personal-first) is exported -- by design.
    rows = [{
        "id": r["id"],
        "name": r["name"] or "",
        "username": r["username"] or "",
        "email": r["primary_email"],
        "provider": r["provider"] or "",
        "gender": r["gender"] or "",
        "country": r["country"] or "",
        "run": r["run"] or 0,
        "created_at": r["created_at"] or "",
        "activity_at": r["activity_at"] or "",
        "phone": r["primary_phone"] or "",
        "whatsapp": bool(r["has_whatsapp"]),
        "messaged": bool(r["messaged_count"]),
    } for r in db.query(sql, params)]

    return {"items": rows, "matched": matched, "options": options}


# ---- scrape runs ----------------------------------------------------------
def run_counts(key: str) -> list[dict]:
    """[{run, count}] for one source, lowest run first, over raw records."""
    return [{"run": int(r["run"]), "count": int(r["n"])} for r in db.query(
        "SELECT run, COUNT(*) AS n FROM records WHERE source = %s "
        "GROUP BY run ORDER BY run ASC", (key,))]


def source_count(key: str) -> int:
    return int(db.scalar("SELECT COUNT(*) AS n FROM contacts WHERE source = %s",
                         (key,), default=0))
