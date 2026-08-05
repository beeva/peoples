#!/usr/bin/env python3
"""MySQL/MariaDB access layer for the contact directory.

This is where the archive lives. The scrapers write into it directly and read
their resume state back out of it; the API serves every request from it.
Filtering, sorting, paging and the facet counts run as SQL against indexed
columns instead of over lists of dicts in Python, which is the point: the cost
of a page stops growing with the size of the archive.

Two shapes of data live in these tables:

  * **Derived** (``contacts``, ``contact_emails``) -- the merge, materialised.
    Rebuildable at any time from ``records``.
  * **Source of truth** (``records``, ``skipped``, ``slack_users``,
    ``enrichment``, ``sent_log``, ``app_state``) -- the only copy. ``records``
    keeps each scraper's original JSON in ``raw`` alongside the normalised
    columns, so the archive can be replayed into a different record shape
    without re-scraping anything.

``contacts`` is a materialised merge: one row per person, with the records
that make them up linked by ``records.contact_id``. Country, gender and the
messaged counters are denormalised onto it so the list can filter and sort on
them with an index; the targeted updates that keep them honest live in
``set_enrichment`` / ``refresh_sent_for_emails``.

Connections are per-thread (the HTTP server is threaded) and reconnect on their
own, so a MySQL restart does not require restarting the API.
"""
import os
import sys
import threading
import time
import unicodedata
from pathlib import Path

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - environment problem, not a code path
    sys.stderr.write(
        "\n[fatal] PyMySQL is not installed.\n"
        "        Run:  npm run setup      (or: pip install -r requirements.txt)\n\n"
    )
    raise

# ---- configuration --------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# The project's own data directory. scripts/mysql-server.js starts mysqld
# against this; `_check_datadir` verifies that whatever answered on the port is
# really serving it.
DATA_DIR = Path(os.environ.get("MYSQL_DATADIR") or (BASE_DIR / "db" / "data"))

DB_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
# 3307 by default, not MySQL's 3306: this is the project's server, and it has to
# be able to run beside whatever XAMPP has on the standard port.
DB_PORT = int(os.environ.get("MYSQL_PORT", "3307"))
DB_USER = os.environ.get("MYSQL_USER", "root")
DB_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
DB_NAME = os.environ.get("MYSQL_DATABASE", "email_scrapper")
# How long to keep retrying at startup. `npm run dev` races the database and
# the API, so the API has to be willing to wait for mysqld to finish booting.
CONNECT_TIMEOUT = int(os.environ.get("MYSQL_CONNECT_TIMEOUT", "60"))

_LOCAL = threading.local()
_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED = False


def _connect(database: str | None = DB_NAME):
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=database, charset="utf8mb4", autocommit=True,
        cursorclass=DictCursor, connect_timeout=10,
        # Large ingests are sent as one multi-row INSERT per chunk; the local
        # socket is fast but the read timeout has to cover a slow disk flush.
        read_timeout=120, write_timeout=120,
    )


def wait_for_server(timeout: int = CONNECT_TIMEOUT) -> None:
    """Block until the server accepts connections, or raise with advice."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            conn = _connect(database=None)
            conn.close()
            return
        except pymysql.Error as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(
        f"could not reach MySQL at {DB_HOST}:{DB_PORT} after {timeout}s ({last}).\n"
        "  Start it with:  npm run db:start\n"
        "  or point MYSQL_BASEDIR in .env at your XAMPP mysql folder."
    )


def connection():
    """This thread's connection, opened on first use and revived if dropped."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except pymysql.Error:
            try:
                conn.close()
            except pymysql.Error:
                pass
    _LOCAL.conn = _connect()
    return _LOCAL.conn


def close_thread_connection() -> None:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except pymysql.Error:
            pass
        _LOCAL.conn = None


# ---- query helpers --------------------------------------------------------
def query(sql: str, params=()) -> list[dict]:
    with connection().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params=()) -> dict | None:
    with connection().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def scalar(sql: str, params=(), default=None):
    row = query_one(sql, params)
    if not row:
        return default
    value = next(iter(row.values()), default)
    return default if value is None else value


def execute(sql: str, params=()) -> int:
    with connection().cursor() as cur:
        return cur.execute(sql, params)


def execute_many(sql: str, rows) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with connection().cursor() as cur:
        return cur.executemany(sql, rows)


class transaction:
    """Wrap a batch of writes so a failure cannot leave a table half-emptied.

    Connections run with autocommit on, which is right for the one-statement
    writes the API does. Re-ingesting a source is the exception: it deletes
    everything for that source before inserting the new rows, and a crash in
    between would lose the source entirely.
    """

    def __enter__(self):
        self.conn = connection()
        self.conn.begin()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            try:
                self.conn.rollback()
            except pymysql.Error:
                pass
        return False


def execute_script(statements) -> None:
    with connection().cursor() as cur:
        for stmt in statements:
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)


# XAMPP ships max_allowed_packet=1M, which a multi-row INSERT of profile bios
# will exceed. Chunks are sized in bytes rather than rows so one unusually
# long record cannot blow the limit on its own.
MAX_CHUNK_BYTES = int(os.environ.get("MYSQL_CHUNK_BYTES", "524288"))


def insert_chunked(sql: str, rows, chunk_bytes: int = MAX_CHUNK_BYTES) -> int:
    """executemany in packet-sized batches. Returns the number of rows sent."""
    batch: list = []
    size = 0
    total = 0
    for row in rows:
        row_bytes = sum(len(v.encode("utf-8", "replace")) if isinstance(v, str)
                        else 8 for v in row)
        if batch and size + row_bytes > chunk_bytes:
            execute_many(sql, batch)
            total += len(batch)
            batch, size = [], 0
        batch.append(row)
        size += row_bytes
    if batch:
        execute_many(sql, batch)
        total += len(batch)
    return total


# ---- schema ---------------------------------------------------------------
# One statement per entry, applied in order and all IF NOT EXISTS, so this
# doubles as the migration path: adding a table here is enough.

# Bump when the shape of an *ingested* table changes. The ingested tables are
# derived from the scraper files, so a version bump just drops and rebuilds
# them -- nothing is lost. Server-owned tables (enrichment, sent_log,
# app_state) are never dropped and would need a real ALTER.
SCHEMA_VERSION = 7

# Tables that dbsync can regenerate from the scraper files at any time.
INGESTED_TABLES = ("contact_emails", "contacts", "records")

SCHEMA = [
    # Every scraped occurrence, normalised to the shape server.py works in.
    # Read by contact (the detail page) and by source (a re-ingest), which is
    # all the indexing it needs -- the list never touches this table.
    """
    CREATE TABLE IF NOT EXISTS records (
      id                   VARCHAR(190) NOT NULL,
      source               VARCHAR(32)  NOT NULL,
      local_id             VARCHAR(190) NOT NULL DEFAULT '',
      contact_id           VARCHAR(190) NULL,
      -- The record's own addresses, in the order the scraper ranked them.
      -- Kept as JSON on the row rather than in a join table: the only reader
      -- is the merge, which wants them as an ordered list per record anyway.
      emails               TEXT         NULL,
      -- The scraper's original record, verbatim. The normalised columns are a
      -- lossy projection of it -- they drop `email_sources`, `followers`,
      -- `region`, `hireable` and anything a scraper starts collecting later --
      -- so without this the JSONL files would be the only copy of those, and
      -- the database could not be replayed into a different record shape.
      raw                  MEDIUMTEXT   NULL,
      name                 VARCHAR(255) NOT NULL DEFAULT '',
      username             VARCHAR(190) NOT NULL DEFAULT '',
      title                VARCHAR(512) NOT NULL DEFAULT '',
      url                  VARCHAR(768) NOT NULL DEFAULT '',
      site_url             VARCHAR(768) NOT NULL DEFAULT '',
      location             VARCHAR(255) NOT NULL DEFAULT '',
      organization         VARCHAR(255) NOT NULL DEFAULT '',
      created_at           VARCHAR(40)  NOT NULL DEFAULT '',
      activity_at          VARCHAR(40)  NOT NULL DEFAULT '',
      run                  INT          NOT NULL DEFAULT 0,
      scraped_country      VARCHAR(96)  NOT NULL DEFAULT '',
      scraped_country_code CHAR(2)      NOT NULL DEFAULT '',
      scraped_gender       VARCHAR(10)  NOT NULL DEFAULT '',
      preview              TEXT         NULL,
      full_text            MEDIUMTEXT   NULL,
      tags                 TEXT         NULL,
      apply_links          TEXT         NULL,
      messaging            TEXT         NULL,
      links                TEXT         NULL,
      ts                   DOUBLE       NOT NULL DEFAULT 0,
      created_ts           DOUBLE       NOT NULL DEFAULT 0,
      search_blob          MEDIUMTEXT   NULL,
      PRIMARY KEY (id),
      KEY idx_records_source (source),
      KEY idx_records_contact (contact_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # The merged person: one row per contact, and what the list actually reads.
    # `id` is the representative record's id, so links made before the database
    # existed still resolve.
    """
    CREATE TABLE IF NOT EXISTS contacts (
      id               VARCHAR(190) NOT NULL,
      source           VARCHAR(32)  NOT NULL,
      primary_email    VARCHAR(190) NOT NULL DEFAULT '',
      -- Mailbox bucket of the primary address (gmail / outlook / company / ...).
      -- Worked out once at ingest so the export dialog's provider pills are a
      -- GROUP BY rather than a Python pass over every row in the source.
      provider         VARCHAR(16)  NOT NULL DEFAULT '',
      emails           TEXT         NULL,
      email_count      INT          NOT NULL DEFAULT 0,
      name             VARCHAR(255) NOT NULL DEFAULT '',
      username         VARCHAR(190) NOT NULL DEFAULT '',
      title            VARCHAR(512) NOT NULL DEFAULT '',
      url              VARCHAR(768) NOT NULL DEFAULT '',
      site_url         VARCHAR(768) NOT NULL DEFAULT '',
      location         VARCHAR(255) NOT NULL DEFAULT '',
      organization     VARCHAR(255) NOT NULL DEFAULT '',
      created_at       VARCHAR(40)  NOT NULL DEFAULT '',
      activity_at      VARCHAR(40)  NOT NULL DEFAULT '',
      run              INT          NOT NULL DEFAULT 0,
      preview          TEXT         NULL,
      tags             TEXT         NULL,
      apply_links      TEXT         NULL,
      messaging        TEXT         NULL,
      links            TEXT         NULL,
      posts            TEXT         NULL,
      post_count       INT          NOT NULL DEFAULT 1,
      ts               DOUBLE       NOT NULL DEFAULT 0,
      created_ts       DOUBLE       NOT NULL DEFAULT 0,
      activity_ts      DOUBLE       NOT NULL DEFAULT 0,
      -- Country/gender as the scraper worked them out; the enrichment fallback.
      scraped_country      VARCHAR(96) NOT NULL DEFAULT '',
      scraped_country_code CHAR(2)     NOT NULL DEFAULT '',
      scraped_gender       VARCHAR(10) NOT NULL DEFAULT '',
      -- Effective values (enrichment first, scraper second). Denormalised so
      -- the country/gender filters and their facet counts can use an index.
      country          VARCHAR(96)  NOT NULL DEFAULT '',
      country_code     CHAR(2)      NOT NULL DEFAULT '',
      gender           VARCHAR(10)  NOT NULL DEFAULT '',
      -- Same reasoning for the sent/unsent filter.
      messaged_count   INT          NOT NULL DEFAULT 0,
      messaged_at      VARCHAR(40)  NOT NULL DEFAULT '',
      messaged_to      VARCHAR(190) NOT NULL DEFAULT '',
      messaged_manual  TINYINT(1)   NOT NULL DEFAULT 0,
      -- Accent-folded, lowercased sort keys: "Angel" has to land next to
      -- "Andres", not after "Zoe", and MySQL collations alone will not do
      -- the blanks-last part.
      name_key         VARCHAR(255) NOT NULL DEFAULT '',
      email_key        VARCHAR(190) NOT NULL DEFAULT '',
      country_key      VARCHAR(96)  NOT NULL DEFAULT '',
      search_blob      MEDIUMTEXT   NULL,
      PRIMARY KEY (id),
      -- One index per *sort*, because an unindexed ORDER BY has to filesort
      -- the whole matching set before it can hand back one page. The filter
      -- columns are deliberately not indexed: gender has three values and
      -- messaged has two, so a scan beats an index lookup on both, and every
      -- index is a write this table pays for on each re-ingest.
      KEY idx_contacts_source_ts (source, ts),
      KEY idx_contacts_source_run (source, run, ts),
      KEY idx_contacts_name_key (source, name_key),
      KEY idx_contacts_email_key (source, email_key),
      KEY idx_contacts_country_key (source, country_key),
      -- Country *is* selective (one of a couple of hundred), and this is also
      -- how a single enrichment result finds the contact it belongs to.
      KEY idx_contacts_source_country (source, country),
      KEY idx_contacts_primary_email (primary_email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Every address belonging to a contact -- "messaged" is true when ANY of
    # them has been written to, which is a join, not a column comparison.
    """
    CREATE TABLE IF NOT EXISTS contact_emails (
      contact_id VARCHAR(190) NOT NULL,
      email      VARCHAR(190) NOT NULL,
      pos        INT          NOT NULL DEFAULT 0,
      PRIMARY KEY (contact_id, email),
      KEY idx_contact_emails_email (email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # People a scrape looked at and ruled out for a reason that does not depend
    # on that run's flags ("no email", "not a user"). Without this the next run
    # re-fetches every one of them -- several API calls each, and most people
    # scanned are ruled out -- to reach the same verdict. Was skipped.jsonl.
    """
    CREATE TABLE IF NOT EXISTS skipped (
      source   VARCHAR(32)  NOT NULL,
      local_id VARCHAR(190) NOT NULL,
      reason   VARCHAR(64)  NOT NULL DEFAULT '',
      seen_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (source, local_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Slack workspace exports. Not scraped by this app -- they are dropped in by
    # hand -- but they are contact data, so they live with the rest of it.
    """
    CREATE TABLE IF NOT EXISTS slack_users (
      workspace_slug VARCHAR(190) NOT NULL,
      workspace_name VARCHAR(255) NOT NULL DEFAULT '',
      user_key       VARCHAR(190) NOT NULL,
      email          VARCHAR(190) NOT NULL DEFAULT '',
      pos            INT          NOT NULL DEFAULT 0,
      data           MEDIUMTEXT   NULL,
      PRIMARY KEY (workspace_slug, user_key),
      KEY idx_slack_email (email),
      KEY idx_slack_ws (workspace_slug, pos)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Claude-inferred country/gender, keyed by the contact's primary address.
    # Was scrapers/enrich_cache.json.
    """
    CREATE TABLE IF NOT EXISTS enrichment (
      email        VARCHAR(190) NOT NULL,
      country      VARCHAR(96)  NOT NULL DEFAULT '',
      country_code CHAR(2)      NOT NULL DEFAULT '',
      gender       VARCHAR(10)  NOT NULL DEFAULT '',
      updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Who we have emailed. Was scrapers/sent.json, which was rewritten in full
    # on every single mark -- 376KB of JSON to record one boolean.
    """
    CREATE TABLE IF NOT EXISTS sent_log (
      email        VARCHAR(190) NOT NULL,
      send_count   INT          NOT NULL DEFAULT 0,
      last_sent    VARCHAR(40)  NOT NULL DEFAULT '',
      last_subject VARCHAR(512) NOT NULL DEFAULT '',
      manual       TINYINT(1)   NOT NULL DEFAULT 0,
      PRIMARY KEY (email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Per-source scraper state (last run, cursor, counts). Was state.json.
    """
    CREATE TABLE IF NOT EXISTS app_state (
      k VARCHAR(64) NOT NULL,
      v LONGTEXT    NULL,
      PRIMARY KEY (k)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # What we last ingested from each scraper file, so an unchanged file is
    # skipped instead of re-parsed and re-inserted on every boot.
    """
    CREATE TABLE IF NOT EXISTS sync_meta (
      source       VARCHAR(32)  NOT NULL,
      file_path    VARCHAR(512) NOT NULL DEFAULT '',
      file_size    BIGINT       NOT NULL DEFAULT 0,
      file_mtime   DOUBLE       NOT NULL DEFAULT 0,
      digest       CHAR(64)     NOT NULL DEFAULT '',
      record_count INT          NOT NULL DEFAULT 0,
      contact_count INT         NOT NULL DEFAULT 0,
      synced_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (source)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


def ensure_database() -> None:
    """Create the database itself (not its tables) if it is not there yet.

    Split out from ``bootstrap`` because restoring a dump needs something to
    connect to before the dump's own CREATE DATABASE statement can run.
    """
    wait_for_server()
    admin = _connect(database=None)
    try:
        with admin.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            # XAMPP's my.ini caps packets at 1MB, which is too small for bulk
            # ingest and for restoring a dump. Raising it here (rather than
            # editing the user's XAMPP config) lasts for the life of this
            # mysqld, which is exactly the window we need it for.
            try:
                cur.execute("SET GLOBAL max_allowed_packet = 67108864")
            except pymysql.Error:
                pass  # not privileged; the chunked inserts still work
    finally:
        admin.close()
    # The thread's cached connection may point at a database that has just been
    # dropped and recreated; drop it so the next call reconnects cleanly.
    close_thread_connection()
    _check_datadir()


def _check_datadir() -> None:
    """Warn if the server on our port is not serving the project's data.

    The port is ours by convention, not by force. If some other MySQL got there
    first -- XAMPP reconfigured onto 3307, a stray service -- every query would
    still succeed while reading an entirely different database, and the archive
    would simply look empty. Better to say so than to let that pass as data
    loss.
    """
    try:
        actual = scalar("SELECT @@datadir AS d", default="") or ""
    except pymysql.Error:
        return
    try:
        same = Path(actual).resolve() == DATA_DIR.resolve()
    except OSError:
        same = False
    if not same:
        print(
            f"[warn] the MySQL on {DB_HOST}:{DB_PORT} is serving\n"
            f"         {actual}\n"
            f"       but this project's data directory is\n"
            f"         {DATA_DIR}\n"
            f"       That is a different database. Stop whatever is on the "
            f"port, or set MYSQL_PORT in .env.",
            file=sys.stderr,
        )


def bootstrap(verbose: bool = True) -> None:
    """Create the database and tables if they are missing. Safe to call twice."""
    global _BOOTSTRAPPED
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        ensure_database()
        # app_state has to exist before we can read the schema version out of it.
        execute_script(SCHEMA)
        _migrate_ingested(verbose)
        _BOOTSTRAPPED = True
        if verbose:
            print(f"[db] {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} ready")


def _migrate_ingested(verbose: bool = True) -> None:
    """Rebuild the ingested tables when their shape has changed.

    They are a cache of the scraper files, so dropping them costs one re-sync
    and nothing else -- far simpler, and far less error-prone, than writing an
    ALTER for every column that moves. The server-owned tables are untouched.
    """
    import json
    row = query_one("SELECT v FROM app_state WHERE k = 'schema_version'")
    try:
        current = int(json.loads(row["v"])) if row else 0
    except (ValueError, TypeError, KeyError):
        current = 0
    if current == SCHEMA_VERSION:
        return
    if current:
        if verbose:
            print(f"[db] schema {current} -> {SCHEMA_VERSION}: rebuilding "
                  f"ingested tables")
        with connection().cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in INGESTED_TABLES:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute("DROP TABLE IF EXISTS `record_emails`")  # retired in v3
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        execute_script(SCHEMA)
        # The files have not changed, so nothing would otherwise re-sync them.
        execute("DELETE FROM sync_meta")
    state_put("schema_version", SCHEMA_VERSION)


class bulk_load:
    """Session settings that make a full re-ingest cheap.

    Every row we insert is one we just deleted, and the source of truth is the
    file on disk -- so the per-statement uniqueness and referential checks are
    verifying something we already know, at a cost that dominates the load.
    Restored on exit, including when the load fails.
    """

    def __enter__(self):
        execute("SET SESSION unique_checks = 0")
        execute("SET SESSION foreign_key_checks = 0")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            execute("SET SESSION unique_checks = 1")
            execute("SET SESSION foreign_key_checks = 1")
        except pymysql.Error:
            pass
        return False


def server_version() -> str:
    return str(scalar("SELECT VERSION() AS v", default="") or "")


def table_counts() -> dict:
    """Row counts per table, for the Database page and `npm run db:status`."""
    out = {}
    for name in ("records", "contacts", "contact_emails", "skipped",
                 "slack_users", "enrichment", "sent_log", "app_state",
                 "sync_meta"):
        try:
            out[name] = int(scalar(f"SELECT COUNT(*) AS n FROM `{name}`", default=0))
        except pymysql.Error:
            out[name] = 0
    return out


def database_size_bytes() -> int:
    return int(scalar(
        "SELECT COALESCE(SUM(data_length + index_length), 0) AS n "
        "FROM information_schema.tables WHERE table_schema = %s",
        (DB_NAME,), default=0) or 0)


# ---- sort keys ------------------------------------------------------------
def text_key(value: str) -> str:
    """Accent-folded lowercase sort key.

    Kept in Python (and stored in a column) rather than left to a MySQL
    collation so the ordering matches, character for character, what the app
    did before the database existed.
    """
    decomposed = unicodedata.normalize("NFKD", (value or "").strip().lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# ---- server-owned state: app_state ----------------------------------------
def state_get_all() -> dict:
    import json
    out = {}
    for row in query("SELECT k, v FROM app_state"):
        try:
            out[row["k"]] = json.loads(row["v"] or "{}")
        except ValueError:
            out[row["k"]] = {}
    return out


def state_put(key: str, value) -> None:
    import json
    execute(
        "INSERT INTO app_state (k, v) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE v = VALUES(v)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


# ---- server-owned state: enrichment ---------------------------------------
def enrichment_get(email: str) -> dict:
    if not email:
        return {}
    row = query_one(
        "SELECT country, country_code, gender FROM enrichment WHERE email = %s",
        (email,))
    return dict(row) if row else {}


def enrichment_count() -> int:
    return int(scalar("SELECT COUNT(*) AS n FROM enrichment", default=0))


def enrichment_has(email: str) -> bool:
    return bool(scalar("SELECT 1 AS n FROM enrichment WHERE email = %s LIMIT 1",
                       (email,), default=0))


def set_enrichment(email: str, enr: dict) -> None:
    """Store an inference and push it onto the contacts it applies to.

    Enrichment is keyed by primary email, and ``contacts.country/gender`` are
    the denormalised copies the filters read -- so writing one without the
    other would leave the list disagreeing with the detail page.
    """
    if not email:
        return
    country = (enr.get("country") or "").strip()
    code = (enr.get("country_code") or "").strip().upper()[:2]
    gender = (enr.get("gender") or "").strip().lower()
    execute(
        "INSERT INTO enrichment (email, country, country_code, gender) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE country = VALUES(country), "
        "  country_code = VALUES(country_code), gender = VALUES(gender), "
        "  updated_at = CURRENT_TIMESTAMP",
        (email, country, code, gender),
    )
    # An empty inference must not erase what the scraper already knew.
    execute(
        "UPDATE contacts SET "
        "  country = COALESCE(NULLIF(%s, ''), scraped_country, ''), "
        "  country_code = COALESCE(NULLIF(%s, ''), scraped_country_code, ''), "
        "  gender = COALESCE(NULLIF(%s, ''), scraped_gender, ''), "
        "  country_key = %s "
        "WHERE primary_email = %s",
        (country, code, gender, text_key(country or ""), email),
    )


def apply_all_enrichment(source: str = "") -> int:
    """Re-apply every cached inference onto contacts. Used after a rebuild.

    Scoped to one source when given, so re-ingesting GitHub does not rewrite
    every about.me row as a side effect.
    """
    where = " AND c.source = %s" if source else ""
    args = (source,) if source else ()
    n = execute(
        "UPDATE contacts c JOIN enrichment e ON e.email = c.primary_email SET "
        "  c.country = COALESCE(NULLIF(e.country, ''), c.scraped_country, ''), "
        "  c.country_code = COALESCE(NULLIF(e.country_code, ''), "
        "                            c.scraped_country_code, ''), "
        "  c.gender = COALESCE(NULLIF(e.gender, ''), c.scraped_gender, '') "
        "WHERE 1 = 1" + where, args)
    # country_key is the accent-folded sort key. Enrichment only ever writes
    # plain ASCII country names, so lowercasing is the same folding here.
    execute("UPDATE contacts c SET c.country_key = LOWER(c.country) "
            "WHERE c.country <> ''" + where, args)
    return n


# ---- server-owned state: sent log -----------------------------------------
def sent_count() -> int:
    return int(scalar("SELECT COUNT(*) AS n FROM sent_log", default=0))


def refresh_sent_for_emails(emails) -> None:
    """Recompute the messaged columns for every contact holding these addresses.

    A contact counts as messaged when *any* of its addresses has been written
    to, and the badge shows the most recent one -- so this aggregates across
    ``contact_emails`` rather than looking at the primary address alone.
    """
    emails = [e for e in {(e or "").strip().lower() for e in emails or []} if e]
    if not emails:
        return
    placeholders = ", ".join(["%s"] * len(emails))
    ids = [r["contact_id"] for r in query(
        f"SELECT DISTINCT contact_id FROM contact_emails "
        f"WHERE email IN ({placeholders})", emails)]
    refresh_sent_for_contacts(ids)


def refresh_sent_for_contacts(contact_ids) -> None:
    contact_ids = [c for c in contact_ids or [] if c]
    if not contact_ids:
        return
    for i in range(0, len(contact_ids), 500):
        batch = contact_ids[i:i + 500]
        placeholders = ", ".join(["%s"] * len(batch))
        # Totals across all of the contact's addresses.
        execute(
            f"UPDATE contacts c LEFT JOIN ("
            f"  SELECT ce.contact_id AS cid, SUM(s.send_count) AS n,"
            f"         MAX(s.last_sent) AS last_sent"
            f"  FROM contact_emails ce JOIN sent_log s ON s.email = ce.email"
            f"  WHERE ce.contact_id IN ({placeholders})"
            f"  GROUP BY ce.contact_id"
            f") agg ON agg.cid = c.id "
            f"SET c.messaged_count = COALESCE(agg.n, 0), "
            f"    c.messaged_at = COALESCE(agg.last_sent, '') "
            f"WHERE c.id IN ({placeholders})",
            batch + batch,
        )
        # Which address that most recent send went to, and whether it was
        # logged by hand. Only meaningful for contacts that have been messaged.
        execute(
            f"UPDATE contacts c JOIN contact_emails ce ON ce.contact_id = c.id "
            f"JOIN sent_log s ON s.email = ce.email AND s.last_sent = c.messaged_at "
            f"SET c.messaged_to = s.email, c.messaged_manual = s.manual "
            f"WHERE c.id IN ({placeholders}) AND c.messaged_count > 0",
            batch,
        )
        execute(
            f"UPDATE contacts SET messaged_to = '', messaged_manual = 0 "
            f"WHERE id IN ({placeholders}) AND messaged_count = 0",
            batch,
        )


def refresh_all_sent(source: str = "") -> None:
    """Rebuild every messaged column from sent_log. Used after a rebuild.

    Scoped to one source when given -- a re-ingest only clears the columns for
    the source it rebuilt, so only that source needs recomputing.
    """
    where = " AND c.source = %s" if source else ""
    args = (source,) if source else ()
    execute("UPDATE contacts c SET c.messaged_count = 0, c.messaged_at = '', "
            "c.messaged_to = '', c.messaged_manual = 0 WHERE 1 = 1" + where, args)
    execute(
        "UPDATE contacts c JOIN ("
        "  SELECT ce.contact_id AS cid, SUM(s.send_count) AS n,"
        "         MAX(s.last_sent) AS last_sent"
        "  FROM contact_emails ce JOIN sent_log s ON s.email = ce.email"
        "  GROUP BY ce.contact_id"
        ") agg ON agg.cid = c.id "
        "SET c.messaged_count = COALESCE(agg.n, 0), "
        "    c.messaged_at = COALESCE(agg.last_sent, '') "
        "WHERE 1 = 1" + where, args)
    execute(
        "UPDATE contacts c JOIN contact_emails ce ON ce.contact_id = c.id "
        "JOIN sent_log s ON s.email = ce.email AND s.last_sent = c.messaged_at "
        "SET c.messaged_to = s.email, c.messaged_manual = s.manual "
        "WHERE c.messaged_count > 0" + where, args)
