#!/usr/bin/env python3
"""MySQL-backed JSON API for browsing scraped contacts.

This is the data API consumed by the Next.js app in web/ (run both with
`npm run dev`). It exposes pagination, full-text search, sorting, **multiple
data sources**, single-contact detail, Claude-generated outreach email, and
**on-demand incremental re-scraping** of each source:

    * discourse, aboutme, github  (and dev.to, stored but not one of the tabs)

**Everything lives in MySQL.** The scrapers insert each record as they collect
it and read their resume state back out of the same tables, so there are no
data files: no JSONL to append, no JSON mirror to rewrite, nothing to re-read
at boot. Every request is answered with SQL, so the cost of a page does not
grow with the size of the archive. Claude's country/gender inferences, the
sent-message log, the ruled-out logins and the per-source scraper state are
rows too.

The database belongs to the project -- `db/data`, `db/my.ini`, port 3307 --
and `npm run dev` starts it alongside this server and the web app. XAMPP
supplies only the mysqld binary; its own databases are untouched.
Configuration comes from a `.env` file next to this script -- see
`.env.example`:

    python server.py            # http://127.0.0.1:8000
    python server.py 9000       # custom port
"""
import json
import os
import smtplib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
SCRAPERS_DIR = BASE_DIR / "scrapers"


def _load_dotenv(path: Path) -> None:
    """Minimal `.env` loader (stdlib only): KEY=VALUE per line.

    Lines may be blank, `# comments`, or `export KEY=VALUE`. Surrounding quotes
    are stripped. A real environment variable always wins over the file, so the
    `.env` is just convenient defaults you can override at the shell.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


# Load .env before importing anything that reads config at import time.
_load_dotenv(BASE_DIR / ".env")

import db          # noqa: E402  -- reads MYSQL_* from the environment
import dbdump      # noqa: E402
import dbquery     # noqa: E402
import dbslack     # noqa: E402
import dbsync      # noqa: E402
import records as rec_mod  # noqa: E402
from dbquery import MAX_PER_PAGE, _to_float, _to_int  # noqa: E402
from records import now_iso as _now_iso  # noqa: E402

SOURCES = rec_mod.SOURCES
SOURCE_BY_KEY = rec_mod.SOURCE_BY_KEY

# ---- outbound messaging (Claude-generated email) --------------------------
# Everything sensitive comes from the environment -- nothing is hardcoded.
# Required to generate:  ANTHROPIC_API_KEY
# Required to send:      SMTP_PASSWORD  (an app password)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
# Display name shown to the recipient (and used for the email sign-off).
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Ephrem")

# ---- contact enrichment (Claude-inferred country + gender) ----------------
# Inferred once per contact (keyed by primary email), stored in the
# `enrichment` table, and filled in lazily by background workers for contacts
# that get viewed.
ENRICH_ENABLED = bool(ANTHROPIC_API_KEY) and \
    os.environ.get("ENRICH", "1").lower() not in ("0", "false", "no")
ENRICH_WORKERS = max(1, int(os.environ.get("ENRICH_WORKERS", "3")))
ENRICH_LOCK = threading.Lock()
ENRICH_QUEUE: deque = deque()
ENRICH_SEEN: set = set()     # emails queued this run (avoid re-enqueue)

# Per-source resume cursors written by the scrapers (git-ignored runtime state).
DISCOURSE_CURSOR = SCRAPERS_DIR / "discourse" / "threejs" / ".cursor"
ABOUTME_CURSOR = SCRAPERS_DIR / "aboutme" / ".cursor"
GITHUB_CURSOR = SCRAPERS_DIR / "github" / ".cursor"
CURSOR_FILES = {"discourse": DISCOURSE_CURSOR, "aboutme": ABOUTME_CURSOR,
                "github": GITHUB_CURSOR}


# ---- persisted per-source state (last_run, counts, cursor) ----------------
# Mirrored in memory because every scrape-status poll reads it; the database
# is still where it lives.
STATE_LOCK = threading.Lock()
STATE = {}


def load_state():
    global STATE
    STATE = db.state_get_all()


def save_state(key: str) -> None:
    try:
        db.state_put(key, STATE.get(key) or {})
    except Exception as e:  # noqa: BLE001 -- state is a convenience, not critical
        print(f"[warn] could not save state for {key}: {e}", file=sys.stderr)


# ---- scrape jobs ----------------------------------------------------------
JOBS_LOCK = threading.Lock()
JOBS = {}  # source -> job dict


def _seed_discourse_cursor() -> int:
    """Highest topic_id already stored -> first-run resume point, so we don't
    re-scrape the topics we already have.

    Read out of each record's preserved raw JSON, which is where the scraper's
    own `topic_id` lives (the normalised columns have no field for it).
    """
    best = 0
    for row in db.query("SELECT raw FROM records WHERE source = 'discourse'"):
        try:
            tid = json.loads(row["raw"] or "{}").get("topic_id")
        except ValueError:
            continue
        if isinstance(tid, int) and tid > best:
            best = tid
    return best


def _scrape_argv(key: str, params: dict) -> list[str]:
    """Build the bounded, incremental command for a source's scraper."""
    py = sys.executable
    if key == "discourse":
        limit = params.get("limit", "300")
        cursor = (STATE.get("discourse") or {}).get("cursor")
        if cursor in (None, ""):
            cursor = _seed_discourse_cursor()
        return [py, str(SCRAPERS_DIR / "discourse" / "discourse_scrape.py"),
                "--limit", str(limit),
                "--since-topic-id", str(cursor or 0),
                "--cursor-out", str(DISCOURSE_CURSOR)]
    if key == "devto":
        pages = params.get("pages", "3")
        argv = [py, str(SCRAPERS_DIR / "devto" / "devto_scrape.py"), "--pages", str(pages)]
        if params.get("full"):
            argv.append("--full")
        return argv
    if key == "aboutme":
        limit = params.get("limit", "50")
        argv = [py, str(SCRAPERS_DIR / "aboutme" / "aboutme_scrape.py"),
                "--limit", str(limit), "--cursor-out", str(ABOUTME_CURSOR)]
        cursor = (STATE.get("aboutme") or {}).get("cursor")
        if cursor:
            argv += ["--start-sitemap", cursor]
        return argv
    if key == "github":
        argv = [py, str(SCRAPERS_DIR / "github" / "github_scrape.py"),
                "--cursor-out", str(GITHUB_CURSOR)]
        # A target is a total ("get me to 1000 users"), so it counts the rows
        # already on disk and the run keeps sweeping until it is met. Without
        # one, a click means "fetch a batch" and 50 is a sane batch.
        target = _to_int(str(params.get("target", "")), 0)
        if target > 0:
            argv += ["--target", str(target)]
        # A per-run cap on top of a target would defeat it -- the run would stop
        # at 50 kept and never reach 1000 -- so the two are not combined.
        limit = _to_int(str(params.get("limit", "")), 0 if target > 0 else 50)
        argv += ["--limit", str(limit if target <= 0 else 0)]
        regions = params.get("regions")
        if regions:
            argv += ["--regions", str(regions)]
        pages = _to_int(str(params.get("pages", "")), 0)
        if pages > 0:
            argv += ["--pages", str(pages)]
        # Narrowing filters, mirroring the UI facet controls: scrape only the
        # countries / account ages / gender being targeted. The scraper applies
        # country + age cheaply (off the profile) and gender via Claude.
        country = str(params.get("country", "")).strip()
        if country:
            argv += ["--countries", country]
        age_min = _to_float(str(params.get("age_min", "")))
        age_max = _to_float(str(params.get("age_max", "")))
        if age_min is not None:
            argv += ["--age-min", str(age_min)]
        if age_max is not None:
            argv += ["--age-max", str(age_max)]
        gender = str(params.get("gender", "")).strip().lower()
        # The UI gender facet is multi-select; the scraper targets one gender,
        # so only forward it when exactly one of male/female is requested.
        genders = [g for g in gender.split(",") if g in ("male", "female")]
        if len(genders) == 1:
            argv += ["--gender", genders[0]]
        # Joined / last-active calendar windows: the UI sends an op + a date;
        # the scraper takes them as --<field>-after / --<field>-before.
        for field in ("joined", "active"):
            op = str(params.get(f"{field}_op", "")).strip().lower()
            date = str(params.get(f"{field}_date", "")).strip()
            if op in ("after", "before") and date:
                argv += [f"--{field}-{op}", date]
        # Explore a fresh, random slice of the world on every click. The walk
        # order is otherwise fixed, so a plain restart re-issues the same first
        # queries -- whose users are already on disk and get skipped -- and
        # wastes the run re-covering old ground. Shuffling heads somewhere new
        # each time; already-scraped users are still de-duped by `done`, so
        # nothing is collected twice. `shuffle=0` opts back into cursor resume.
        if str(params.get("shuffle", "1")) not in ("0", "false", "no"):
            argv.append("--shuffle")
        else:
            cursor = (STATE.get("github") or {}).get("cursor")
            if cursor:
                argv += ["--start-location", cursor]
        return argv
    raise ValueError(f"unknown source: {key}")


def _terminate(proc):
    """Best-effort terminate a subprocess (terminate, then kill on grace timeout)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _live_count(key: str) -> int:
    """How many contacts the source holds right now.

    The scraper writes each record to the database as it collects it, so there
    is nothing to ingest here -- only the cached aggregates to drop, so the
    next page reflects what has just arrived.
    """
    dbquery.invalidate_caches()
    return dbquery.source_count(key)


def _run_scrape(key: str, params: dict):
    """Run a scraper subprocess, stream its log, ingest while it runs, and
    ingest once more when it finishes (or is stopped)."""
    job = JOBS[key]
    before = dbquery.source_count(key)
    try:
        argv = _scrape_argv(key, params)
    except ValueError as e:
        job.update(status="error", finished_at=_now_iso(), message=str(e))
        return

    job["log"].append(f"$ {' '.join(argv)}")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
    except OSError as e:
        job.update(status="error", finished_at=_now_iso(), message=f"failed to start: {e}")
        return

    job["proc"] = proc
    job["pid"] = proc.pid
    # If a stop landed between start_scrape() and Popen, honour it immediately.
    if job.get("stop_requested"):
        _terminate(proc)

    # Watchdog: the scraper stores each record as it lands, so this only has to
    # refresh the running count (and drop the cached aggregates) for the UI.
    stop_evt = threading.Event()

    def watch():
        while not stop_evt.wait(2.0):
            try:
                cnt = _live_count(key)
                job["added"] = cnt - before
                job["total"] = cnt
            except Exception:  # noqa: BLE001 -- never let the watchdog die
                pass

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            job["log"].append(line)
    rc = proc.wait()
    stop_evt.set()
    watcher.join(timeout=5)
    job["proc"] = None

    after = _live_count(key)
    added = after - before
    stopped = bool(job.get("stop_requested"))
    if stopped:
        status, message = "stopped", f"stopped · +{added} new ({after} total)"
    elif rc == 0:
        status, message = "done", f"+{added} new ({after} total)"
    else:
        status, message = "error", f"scraper exited with code {rc}"
    job.update(
        status=status,
        finished_at=_now_iso(),
        returncode=rc,
        added=added,
        total=after,
        message=message,
    )

    cursor = None
    cursor_file = CURSOR_FILES.get(key)
    if cursor_file and cursor_file.exists():
        try:
            cursor = cursor_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            cursor = None
    with STATE_LOCK:
        prev = STATE.get(key) or {}
        STATE[key] = {
            "last_run": job["finished_at"],
            "last_status": job["status"],
            "added": added,
            "total": after,
            "cursor": cursor or prev.get("cursor"),
        }
    save_state(key)


def start_scrape(key: str, params: dict) -> dict:
    """Start a background scrape for `key` unless one is already running."""
    if key not in SOURCE_BY_KEY:
        return {"ok": False, "error": f"unknown source '{key}' (pick a specific site)"}
    with JOBS_LOCK:
        cur = JOBS.get(key)
        if cur and cur.get("status") == "running":
            return {"ok": False, "error": "already running", "job": _job_view(key)}
        JOBS[key] = {
            "source": key,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "added": None,
            "total": None,
            "returncode": None,
            "message": "",
            "pid": None,
            "proc": None,
            "stop_requested": False,
            "log": deque(maxlen=300),
        }
    t = threading.Thread(target=_run_scrape, args=(key, params), daemon=True)
    t.start()
    return {"ok": True, "job": _job_view(key)}


def stop_scrape(key: str) -> dict:
    """Request a running scrape to stop. Partial progress already on disk is kept."""
    if key not in SOURCE_BY_KEY:
        return {"ok": False, "error": f"unknown source '{key}'"}
    with JOBS_LOCK:
        job = JOBS.get(key)
        if not job or job.get("status") != "running":
            return {"ok": False, "error": "not running"}
        job["stop_requested"] = True
        proc = job.get("proc")
    _terminate(proc)  # if proc is None (not spawned yet), _run_scrape stops it on start
    return {"ok": True, "job": _job_view(key)}


def _job_view(key: str) -> dict:
    """Serialisable snapshot of a job + its persisted state."""
    job = JOBS.get(key)
    state = STATE.get(key) or {}
    view = {
        "source": key,
        "status": (job or {}).get("status", "idle"),
        "last_run": state.get("last_run"),
        "last_status": state.get("last_status"),
        "last_added": state.get("added"),
        "total": state.get("total"),
    }
    if job:
        view.update({
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "added": job.get("added"),
            "message": job.get("message"),
            "log": list(job.get("log", []))[-30:],
        })
    return view


# ---- merging scrape runs --------------------------------------------------
# A "step" is the scrape run that found a contact. Only sources whose scraper
# stamps a run number have steps to merge.
RUN_SOURCES = {"github"}
MERGE_LOCK = threading.Lock()


def merge_runs(key: str, from_runs: list[int], into: int) -> dict:
    """Relabel every contact collected on `from_runs` as belonging to `into`.

    Two scrapes that were really one piece of work (a run that died half way and
    the one that finished it) should read as one step. Merging never touches the
    contacts themselves, only their step label -- which is now a column, so this
    is an UPDATE rather than a rewrite of the whole archive.
    """
    if key not in RUN_SOURCES:
        return {"ok": False, "status": 400,
                "error": f"'{key or 'all'}' does not number its scrape runs"}
    if into <= 0:
        return {"ok": False, "status": 400, "error": "pick a run to merge into"}
    wanted = {r for r in from_runs if r > 0 and r != into}
    if not wanted:
        return {"ok": False, "status": 400,
                "error": "pick a different run to merge in"}

    # A running scrape is still assigning its own run number; relabelling
    # underneath it would put some of its contacts on the wrong step.
    with JOBS_LOCK:
        job = JOBS.get(key)
        if job and job.get("status") == "running":
            return {"ok": False, "status": 409,
                    "error": "a scrape is running -- stop it first"}

    with MERGE_LOCK:
        present = dbsync.runs_present(key)
        if into not in present:
            return {"ok": False, "status": 404,
                    "error": f"run {into} has no contacts"}
        missing = sorted(wanted - present)
        if missing:
            return {"ok": False, "status": 404,
                    "error": "no contacts on run "
                             + ", ".join(str(m) for m in missing)}
        moved = dbsync.relabel_runs(key, wanted, into)

    dbquery.invalidate_caches()
    return {"ok": True, "moved": moved, "into": into,
            "merged": sorted(wanted), "total": dbquery.source_count(key),
            "runs": dbquery.run_counts(key)}


# ---- Claude helpers (structured JSON via the Messages API) -----------------
def _claude_json(system: str, user: str, schema: dict, max_tokens: int = 1024) -> dict:
    """One Claude call constrained to `schema` via output_config.format.

    Returns the parsed JSON object. Raises RuntimeError on transport/API errors
    and json.JSONDecodeError if the (guaranteed-JSON) text somehow won't parse.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server")
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Claude API error {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach Claude API: {e.reason}")
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined the request")
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    return json.loads(text)


# ---- Claude-generated outreach + email sending ----------------------------
GEN_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def _contact_context(view: dict) -> str:
    """A compact, factual brief about the contact for the prompt (their posts)."""
    lines = []
    for p in (view.get("posts_full") or [])[:5]:
        title = (p.get("title") or "").strip()
        snippet = " ".join((p.get("text") or "").split())[:400]
        if title or snippet:
            lines.append(f"- {title}: {snippet}".strip(" -:"))
    return "\n".join(lines) or "(no posts or profile text captured)"


def generate_message(view: dict, intent: str, tone: str) -> dict:
    """Ask Claude for a personalised {subject, body}. Raises on any failure."""
    name = view.get("name") or view.get("username") or "there"
    system = (
        "You write short, personalised cold-outreach emails. The body must be "
        "plain text (no markdown), 80-150 words, with a greeting that uses the "
        "recipient's name and a brief sign-off using the sender's name. "
        "Reference the recipient's own work when it is relevant; never invent "
        "facts about them."
    )
    user = (
        f"Recipient name: {name}\n"
        f"Sender name (sign the email off as this): {MAIL_FROM_NAME}\n"
        f"Source: {view.get('source')}\n"
        f"Context from their posts/profile:\n{_contact_context(view)}\n\n"
        f"Desired tone: {tone or 'friendly and professional'}\n"
        f"Goal of the email / what to say:\n"
        f"{intent or 'A brief, warm introduction and an invitation to connect.'}"
    )
    try:
        obj = _claude_json(system, user, GEN_SCHEMA, max_tokens=1024)
    except json.JSONDecodeError:
        raise RuntimeError("Claude returned malformed output")
    return {"subject": (obj.get("subject") or "").strip(),
            "body": (obj.get("body") or "").strip()}


def send_email(to_addr: str, subject: str, body: str) -> None:
    """Send a plain-text email from MAIL_FROM via SMTP. Raises on failure."""
    if not SMTP_PASSWORD:
        raise RuntimeError("SMTP_PASSWORD is not set on the server (an app password)")
    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM)) if MAIL_FROM_NAME else MAIL_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body or "")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        err = e.smtp_error
        detail = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        hint = ("SMTP login failed. The provider rejected authentication. ")
        if "5.7.139" in detail or "SmtpClientAuthentication is disabled" in detail:
            hint = ("SMTP login failed: SMTP AUTH is disabled for this mailbox, so "
                    "no password will work over SMTP. Enable 'Authenticated SMTP' "
                    "(Microsoft 365 admin), or switch SMTP_HOST/USER/PASSWORD to a "
                    "provider that allows app-password SMTP (e.g. Gmail). ")
        raise RuntimeError(hint + (f"(server said: {e.smtp_code} {detail})" if detail else ""))
    except (smtplib.SMTPException, OSError) as e:
        raise RuntimeError(f"could not send email: {e}")


# ---- contact enrichment: infer country + gender, background-filled --------
ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "country": {"type": "string"},
        "country_code": {"type": "string"},
        "gender": {"type": "string", "enum": ["male", "female", "unknown"]},
    },
    "required": ["country", "country_code", "gender"],
    "additionalProperties": False,
}


def _infer_country_gender(item: dict) -> dict:
    """Ask Claude to infer country + gender from a contact's sparse signals."""
    system = (
        "You infer a person's likely country and gender from sparse signals: "
        "their name, any stated location, and text they wrote. Reply with JSON. "
        "country = English country name or 'Unknown'. country_code = its ISO "
        "3166-1 alpha-2 code (uppercase) or '' if unknown. gender = 'male', "
        "'female', or 'unknown'. A clearly gendered given name or an explicit "
        "location is enough; when signals are weak prefer Unknown over guessing."
    )
    user = (
        f"Name: {item.get('name') or '(unknown)'}\n"
        f"Stated location: {item.get('location') or '(none)'}\n"
        f"Text they wrote:\n{(item.get('context') or '')[:800]}"
    )
    obj = _claude_json(system, user, ENRICH_SCHEMA, max_tokens=200)
    gender = str(obj.get("gender") or "unknown").lower()
    if gender not in ("male", "female", "unknown"):
        gender = "unknown"
    return {
        "country": (obj.get("country") or "").strip(),
        "country_code": (obj.get("country_code") or "").strip().upper()[:2],
        "gender": gender,
    }


def _enqueue_rows(rows) -> None:
    """Queue the contacts on a page for background inference (once per email).

    Only contacts that have no inference yet are queued, and only ones whose
    country or gender is still blank -- a GitHub profile that already stated
    its location does not need Claude to guess at it.
    """
    if not ENRICH_ENABLED:
        return
    for row in rows or []:
        email = row.get("primary_email") or ""
        if not email:
            continue
        if row.get("country") and row.get("gender"):
            continue
        with ENRICH_LOCK:
            if email in ENRICH_SEEN:
                continue
            ENRICH_SEEN.add(email)
            ENRICH_QUEUE.append({
                "email": email,
                "name": row.get("name") or row.get("username") or "",
                "location": row.get("location") or "",
                "context": row.get("preview") or "",
            })


def _enrich_worker() -> None:
    """Drain the queue, infer per contact, and store it against the email."""
    while True:
        try:
            item = ENRICH_QUEUE.popleft()
        except IndexError:
            time.sleep(0.5)
            continue
        email = item["email"]
        try:
            if db.enrichment_has(email):
                continue
            enr = _infer_country_gender(item)
        except Exception:  # noqa: BLE001 -- transient (rate limit/network); retry later
            with ENRICH_LOCK:
                ENRICH_SEEN.discard(email)  # allow a future re-enqueue
            time.sleep(1.5)
            continue
        try:
            # Writes the inference AND pushes it onto the contacts it applies to.
            db.set_enrichment(email, enr)
            dbquery.invalidate_caches()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not store enrichment for {email}: {e}",
                  file=sys.stderr)
        time.sleep(0.2)  # gentle throttle


# ---- sent-message log: record sends, mark messaged contacts ---------------
def _record_sent(to_addr: str, subject: str) -> None:
    """Log a successful send and mark the matching contact(s) as messaged."""
    key = (to_addr or "").strip().lower()
    if not key:
        return
    db.execute(
        "INSERT INTO sent_log (email, send_count, last_sent, last_subject, manual) "
        "VALUES (%s, 1, %s, %s, 0) "
        "ON DUPLICATE KEY UPDATE send_count = send_count + 1, "
        "  last_sent = VALUES(last_sent), last_subject = VALUES(last_subject), "
        "  manual = 0",
        (key, _now_iso(), (subject or "").strip()[:512]))
    db.refresh_sent_for_emails([key])
    dbquery.invalidate_caches()


def _mark_sent(rec_id: str, sent: bool) -> dict | None:
    """Flip a contact's sent flag by hand, for emails sent outside this app.

    Marking sent logs the primary address; marking unsent forgets every address
    of the contact, including real sends -- that is what "unsent" has to mean
    for the badge and the sent/unsent filter to agree with each other.
    """
    emails = dbquery.contact_emails(rec_id or "")
    if not emails:
        # The sent log is keyed by recipient address, so a contact reachable
        # only by phone has nothing to key on. Distinguished from "no such
        # contact" by the caller, which would otherwise report the wrong thing.
        return {"_no_email": True} if dbquery.contact_row(rec_id or "") else None
    _apply_marks([emails], sent)
    row = dbquery.contact_row(rec_id)
    if row is None:
        return None
    return {
        "id": row["id"],
        "messaged": bool(row["messaged_count"]),
        "messaged_count": row["messaged_count"],
        "messaged_at": row["messaged_at"] or "",
        "messaged_to": row["messaged_to"] or "",
        "messaged_manual": bool(row["messaged_manual"]),
    }


def _apply_marks(email_lists: list[list[str]], sent: bool) -> None:
    """Set/clear the sent log for a batch of contacts, then refresh their rows."""
    touched: set[str] = set()
    if sent:
        # A manual mark must not overwrite a real send's subject or count --
        # hence the guarded update rather than a plain REPLACE.
        rows = [(emails[0], _now_iso()) for emails in email_lists if emails]
        for email, stamp in rows:
            db.execute(
                "INSERT INTO sent_log (email, send_count, last_sent, "
                "  last_subject, manual) VALUES (%s, 1, %s, '', 1) "
                "ON DUPLICATE KEY UPDATE "
                # A real send already recorded here keeps its stamp, its
                # subject and its non-manual flag. Assignments are evaluated
                # left to right and see the values assigned before them, so
                # the two guards on send_count have to come before it is
                # raised to 1 -- otherwise they always read the new value.
                "  last_sent = IF(send_count > 0, last_sent, VALUES(last_sent)), "
                "  manual = IF(send_count > 0, manual, 1), "
                "  send_count = GREATEST(send_count, 1)",
                (email, stamp))
            touched.add(email)
    else:
        flat = sorted({e for emails in email_lists for e in emails})
        for i in range(0, len(flat), 500):
            chunk = flat[i:i + 500]
            marks = ", ".join(["%s"] * len(chunk))
            db.execute(f"DELETE FROM sent_log WHERE email IN ({marks})", chunk)
            touched.update(chunk)
    db.refresh_sent_for_emails(touched)
    dbquery.invalidate_caches()


def _mark_sent_many(ids: list[str], sent: bool) -> int:
    """Bulk _mark_sent (the export dialog's "mark all as sent"): same rules,
    but one refresh pass instead of one per contact."""
    email_lists = []
    for rec_id in ids:
        emails = dbquery.contact_emails(rec_id or "")
        if emails:
            email_lists.append(emails)
    if email_lists:
        _apply_marks(email_lists, sent)
    return len(email_lists)


class Handler(BaseHTTPRequestHandler):
    server_version = "ContactDirectory/4.0"

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, filename: str) -> None:
        """Stream a file back as a download (used for the .sql export)."""
        try:
            size = path.stat().st_size
            fh = path.open("rb")
        except OSError as e:
            self._send_json({"ok": False, "error": str(e)}, 500)
            return
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", "application/sql; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _read_body_bytes(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send_json({
                "service": "email-scrapper data API",
                "storage": f"mysql://{db.DB_HOST}:{db.DB_PORT}/{db.DB_NAME}",
                "ui": "Run the Next.js app in web/ (npm run dev); it consumes this API.",
                "endpoints": [
                    "/api/emails", "/api/email?id=", "/api/export", "/api/stats",
                    "/api/scrape", "/api/scrape/status", "/api/scrape/stop",
                    "/api/message/generate", "/api/message/send",
                    "/api/message/mark", "/api/runs/merge",
                    "/api/slack/workspaces", "/api/slack/users?ws=",
                    "/api/db/status", "/api/db/export", "/api/db/import",
                    "/api/db/import-files",
                ],
            })
            return

        if route == "/api/stats":
            source = params.get("source", ["all"])[0]
            self._send_json({"stats": dbquery.stats_for(source),
                             "sources": dbquery.source_list()})
            return

        if route == "/api/emails":
            page = _to_int(params.get("page", ["1"])[0], 1)
            per_page = _to_int(params.get("per_page", ["12"])[0], 12)
            per_page = max(1, min(per_page, MAX_PER_PAGE))
            self._send_json(dbquery.query_records(
                params.get("source", ["all"])[0],
                params.get("q", [""])[0],
                params.get("sort", ["newest"])[0],
                page, per_page,
                params.get("messaged", ["all"])[0],
                country=params.get("country", [""])[0],
                gender=params.get("gender", [""])[0],
                age_min=params.get("age_min", [""])[0],
                age_max=params.get("age_max", [""])[0],
                runs=params.get("runs", [""])[0],
                joined_op=params.get("joined_op", [""])[0],
                joined_date=params.get("joined_date", [""])[0],
                active_op=params.get("active_op", [""])[0],
                active_date=params.get("active_date", [""])[0],
                contactable=params.get("contactable", [""])[0],
                on_page=_enqueue_rows,
            ))
            return

        if route == "/api/export":
            self._send_json(dbquery.export_records(
                params.get("source", ["github"])[0],
                gender=params.get("gender", [""])[0],
                active_op=params.get("active_op", [""])[0],
                active_date=params.get("active_date", [""])[0],
                joined_op=params.get("joined_op", [""])[0],
                joined_date=params.get("joined_date", [""])[0],
                runs=params.get("runs", [""])[0],
                country=params.get("country", [""])[0],
                messaged=params.get("messaged", [""])[0],
                provider=params.get("provider", [""])[0],
                contactable=params.get("contactable", [""])[0],
                limit=_to_int(params.get("limit", ["0"])[0], 0),
            ))
            return

        if route == "/api/email":
            rec_id = params.get("id", [""])[0]
            view = dbquery.detail_record(rec_id)
            if view is None:
                self._send_json({"error": "not found"}, 404)
            else:
                row = dbquery.contact_row(rec_id)
                if row:
                    _enqueue_rows([row])
                self._send_json(view)
            return

        if route == "/api/scrape/status":
            source = params.get("source", [""])[0]
            if source:
                self._send_json(_job_view(source))
            else:
                self._send_json({"jobs": [_job_view(s["key"]) for s in SOURCES]})
            return

        if route == "/api/slack/workspaces":
            self._send_json({"workspaces": dbslack.workspaces(),
                             "unique_people": dbslack.count_all()})
            return

        if route == "/api/slack/users":
            slug = params.get("ws", [""])[0]
            if not slug:
                self._send_json({"error": "missing ws"}, 400)
            else:
                self._send_json({"ws": slug, "users": dbslack.users(slug)})
            return

        if route == "/api/db/status":
            self._send_json(dbdump.status())
            return

        if route == "/api/db/export":
            # Written to disk first so the response can carry a Content-Length
            # and the browser shows real download progress.
            try:
                result = dbdump.export_sql(params.get("file", [""])[0] or None)
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)}, 500)
                return
            if params.get("download", ["1"])[0] in ("1", "true", "yes"):
                self._send_file(Path(result["path"]), result["filename"])
            else:
                self._send_json({"ok": True, **result})
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        source = params.get("source", [""])[0]

        if parsed.path == "/api/scrape/stop":
            result = stop_scrape(source)
            self._send_json(result, status=200 if result.get("ok") else 409)
            return

        if parsed.path == "/api/message/generate":
            data = self._read_json_body()
            view = dbquery.detail_record(data.get("id") or "")
            if view is None:
                self._send_json({"ok": False, "error": "unknown contact id"}, 404)
                return
            try:
                result = generate_message(view, data.get("intent", ""),
                                          data.get("tone", ""))
                self._send_json({"ok": True, **result})
            except Exception as e:  # noqa: BLE001 -- surface the reason to the UI
                self._send_json({"ok": False, "error": str(e)}, 502)
            return

        if parsed.path == "/api/message/mark":
            data = self._read_json_body()
            # Bulk form: {"ids": [...], "sent": true} -- used by the export
            # dialog to flag everything it just exported in one call.
            ids = data.get("ids")
            if isinstance(ids, list):
                n = _mark_sent_many([str(i) for i in ids],
                                    bool(data.get("sent", True)))
                self._send_json({"ok": True, "marked": n})
                return
            view = _mark_sent(data.get("id") or "", bool(data.get("sent")))
            if view is None:
                self._send_json({"ok": False, "error": "unknown contact id"}, 404)
            elif view.get("_no_email"):
                self._send_json({"ok": False, "error": "this contact has no email "
                                 "address -- the sent log is keyed by address"}, 409)
            else:
                self._send_json({"ok": True, **view})
            return

        if parsed.path == "/api/message/send":
            data = self._read_json_body()
            to_addr = (data.get("to") or "").strip()
            if not to_addr and data.get("id"):
                emails = dbquery.contact_emails(data["id"])
                if emails:
                    to_addr = emails[0]
            if not to_addr:
                self._send_json({"ok": False, "error": "no recipient address"}, 400)
                return
            try:
                send_email(to_addr, data.get("subject", ""), data.get("body", ""))
                _record_sent(to_addr, data.get("subject", ""))
                self._send_json({"ok": True, "to": to_addr})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)}, 502)
            return

        if parsed.path == "/api/runs/merge":
            data = self._read_json_body()
            raw = data.get("from")
            if not isinstance(raw, list):
                raw = [raw]
            result = merge_runs(
                source or str(data.get("source") or ""),
                [_to_int(str(x), 0) for x in raw],
                _to_int(str(data.get("into")), 0),
            )
            status = result.pop("status", 200)
            self._send_json(result, status=status)
            return

        if parsed.path == "/api/scrape":
            scrape_params = {}
            for k in ("pages", "limit", "regions", "target",
                      "country", "gender", "age_min", "age_max",
                      "joined_op", "joined_date", "active_op", "active_date"):
                if k in params:
                    scrape_params[k] = params[k][0]
            if params.get("full", ["0"])[0] in ("1", "true", "yes"):
                scrape_params["full"] = True
            result = start_scrape(source, scrape_params)
            self._send_json(result, status=200 if result.get("ok") else 409)
            return

        if parsed.path == "/api/db/import-files":
            # One-way import of legacy scraper files, for an archive that
            # predates the database. Scrapers write straight to MySQL now, so
            # this is only ever needed once (and does nothing without files).
            force = params.get("force", ["0"])[0] in ("1", "true", "yes")
            try:
                result = dbsync.import_files(force=force, verbose=False)
                dbquery.invalidate_caches()
                self._send_json({"ok": True, **result, "db": dbdump.status()})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if parsed.path == "/api/db/import":
            # Either an uploaded .sql body, or {"path": "..."} naming a file
            # already on this machine (what `npm run db:import` writes).
            body = self._read_body_bytes()
            try:
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    data = json.loads(body.decode("utf-8") or "{}")
                    result = dbdump.import_sql(path=data.get("path") or "")
                else:
                    result = dbdump.import_sql(sql_bytes=body)
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)}, 400)
                return
            # Everything the API had cached describes the old contents.
            dbquery.invalidate_caches()
            load_state()
            self._send_json({"ok": True, **result, "db": dbdump.status()})
            return

        self._send_json({"error": "not found"}, 404)


def main():
    port = _to_int(sys.argv[1], 8000) if len(sys.argv) > 1 else 8000

    # MySQL may still be starting (npm run dev launches us in parallel with it),
    # so this waits rather than failing.
    db.bootstrap()
    # Both are no-ops once the archive is in the database: the scrapers write
    # there directly, and the legacy files are only read while they still exist
    # and their table is still empty.
    dbsync.migrate_legacy_json()
    dbsync.import_files()
    # A schema migration drops the merged contacts (they are a view of
    # `records`); recomputing them is the one thing it cannot do itself.
    dbsync.ensure_contacts_rebuilt()
    load_state()
    dbquery.invalidate_caches()

    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, Handler)
    counts = ", ".join(f"{s['key']}={dbquery.source_count(s['key'])}"
                       for s in SOURCES)
    total = dbquery.source_list()[0]["count"]
    print(f"Loaded {total} contacts ({counts})")
    msg_gen = "on" if ANTHROPIC_API_KEY else "OFF (set ANTHROPIC_API_KEY)"
    msg_send = "on" if SMTP_PASSWORD else "OFF (set SMTP_PASSWORD)"
    print(f"Messaging: generate={msg_gen}, send={msg_send}, from={MAIL_FROM} "
          f"({db.sent_count()} recipients messaged)")
    if ENRICH_ENABLED:
        for _ in range(ENRICH_WORKERS):
            threading.Thread(target=_enrich_worker, daemon=True).start()
        print(f"Enrichment: on ({db.enrichment_count()} cached, "
              f"{ENRICH_WORKERS} workers)")
    else:
        print("Enrichment: off (needs ANTHROPIC_API_KEY; ENRICH=0 to disable)")
    print(f"Serving on http://{addr[0]}:{addr[1]}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
