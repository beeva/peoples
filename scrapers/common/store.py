#!/usr/bin/env python3
"""The scrapers' write end: records go straight into MySQL.

Every scraper used to append JSONL and work out what it already had by reading
that file back. Both halves now talk to the database instead:

    store = RecordStore("github")
    done  = store.done_keys()          # was load_done_keys(path, "login")
    run   = store.next_run()           # was _next_run(path)
    ...
    store.add(rec)                     # was out.write(json.dumps(rec) + "\\n")

``add`` normalises the record, merges it into the contact it belongs to, and
commits — so a scrape is visible in the UI the moment each profile lands,
rather than after a file is re-read. The scraper's own dict is stored verbatim
alongside the normalised columns, so nothing it collects is lost to the shape
the app happens to want today.

Scrapers run as standalone scripts from anywhere, so this module puts the
project root on ``sys.path`` before importing the database layer.
"""
import sys
from pathlib import Path

# scrapers/common/store.py -> project root is two levels up.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import db        # noqa: E402
import dbsync    # noqa: E402


def _load_project_env() -> None:
    """Read the root .env so a scraper run by hand finds the same database.

    server.py loads it for its own process; a scraper started straight from a
    terminal has no such parent, and would otherwise fall back to the defaults
    and quietly write somewhere else.
    """
    path = _ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    import os
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


class RecordStore:
    """A source's write handle on the archive.

    Use it as a context manager so a partial run still commits what it got --
    a scrape that is stopped half way should keep its profiles, which is what
    the Stop button has always promised.
    """

    def __init__(self, source: str, *, batch: int = 1):
        _load_project_env()
        db.bootstrap(verbose=False)
        # A scraper is often the first thing run after a schema bump, and the
        # bump drops the merged contacts. Rebuilding them before writing keeps
        # the incremental merge working against a full table instead of
        # silently re-deriving the archive one new record at a time.
        dbsync.ensure_contacts_rebuilt(verbose=False)
        if source not in dbsync.rec_mod.SOURCE_BY_KEY:
            raise ValueError(f"unknown source: {source}")
        self.source = source
        # Records per write. One means the UI sees each profile as it lands,
        # which is what a network-bound scrape wants; a bulk importer can raise
        # it to trade that for throughput.
        self.batch = max(1, batch)
        self._pending: list[dict] = []
        self._added = 0

    # ---- what we already have --------------------------------------------
    def done_keys(self) -> set:
        """The scraper keys already stored (logins, usernames, post ids)."""
        return dbsync.done_local_ids(self.source)

    def next_run(self) -> int:
        return dbsync.next_run(self.source)

    def count(self) -> int:
        return dbsync.source_record_count(self.source)

    # ---- writing ----------------------------------------------------------
    def add(self, raw: dict) -> None:
        """Store one scraped record."""
        self._pending.append(raw)
        self._added += 1
        if len(self._pending) >= self.batch:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        dbsync.ingest_records(self.source, pending,
                              first_index=self._added - len(pending) + 1)

    # ---- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self.flush()
        db.close_thread_connection()

    def __enter__(self) -> "RecordStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Flush even when the scrape raised or was interrupted: the records
        # already collected are good, and losing them would make a stopped run
        # worse than a slow one.
        try:
            self.flush()
        finally:
            db.close_thread_connection()
        return False


class SkipStore:
    """Logins a scrape looked at and ruled out, so the next run skips them.

    Only *filter-independent* verdicts belong here -- "no email", "not a user".
    A verdict that depends on the flags a run was given ("off-country") would
    wrongly hide people from a later run that wanted them.
    """

    def __init__(self, source: str):
        _load_project_env()
        db.bootstrap(verbose=False)
        self.source = source

    def keys(self) -> set:
        return {r["local_id"] for r in db.query(
            "SELECT local_id FROM skipped WHERE source = %s", (self.source,))}

    def add(self, local_id: str, reason: str) -> None:
        db.execute(
            "INSERT INTO skipped (source, local_id, reason) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE reason = VALUES(reason), "
            "  seen_at = CURRENT_TIMESTAMP",
            (self.source, str(local_id)[:190], (reason or "")[:64]),
        )

    def close(self) -> None:
        db.close_thread_connection()
