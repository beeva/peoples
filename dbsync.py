#!/usr/bin/env python3
"""Writing scraped records into MySQL, and merging them into contacts.

``ingest_records`` is the single write path into the archive. The scrapers call
it through ``scrapers/common/store.py`` as each record is collected; it is
incremental by construction, rebuilding only the contacts the new records
actually touch, so the cost is proportional to what arrived rather than to what
is already stored.

The rest of this module imports *legacy* data files -- the JSONL/JSON an
archive collected before the database existed. Scrapers no longer produce them,
so on a migrated setup this does nothing. Files are fingerprinted (size, mtime,
SHA-256) so an unchanged one is skipped, and one that has only been appended to
has just its new lines read.

Claude's enrichment and the sent log are keyed by email and live only in the
database, so rebuilding contacts re-applies them rather than discarding them.

    python dbsync.py            # import any legacy files still on disk
    python dbsync.py --force    # re-import them from the start
    python dbsync.py github     # one source
"""
import hashlib
import json
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import db
import records as rec_mod
from common import email_provider
from common.phones import phone_first

# How much of a file to read per hash update. The GitHub file is 7MB today and
# only grows, so it is read in chunks rather than slurped.
_HASH_CHUNK = 1 << 20

# One sync at a time per source. During a scrape the watchdog syncs on a timer
# while the API is still serving, and the Database page can ask for one too --
# two overlapping rebuilds of the same source would each delete the rows the
# other just inserted.
_SYNC_LOCKS: dict = defaultdict(threading.Lock)
_LOCKS_GUARD = threading.Lock()


def _lock_for(source: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _SYNC_LOCKS[source]


def fingerprint(path: Path) -> dict:
    """Size, mtime and content hash of a scraper file (zeros if it is missing).

    The hash is what actually decides a re-ingest: mtime alone would re-sync
    after a `touch`, and size alone would miss an in-place edit such as the
    run-number rewrite that `merge_runs` performs.
    """
    try:
        stat = path.stat()
    except OSError:
        return {"size": 0, "mtime": 0.0, "digest": ""}
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return {"size": 0, "mtime": 0.0, "digest": ""}
    return {"size": stat.st_size, "mtime": stat.st_mtime, "digest": h.hexdigest()}


def _stored_meta(source: str) -> dict:
    row = db.query_one("SELECT * FROM sync_meta WHERE source = %s", (source,))
    return dict(row) if row else {}


def _quick_stat(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {"size": 0, "mtime": 0.0}
    return {"size": st.st_size, "mtime": st.st_mtime}


def is_stale(source: str, meta: dict | None = None) -> bool:
    """True when the source's file differs from what we last ingested.

    The scrape watchdog asks this every couple of seconds, so an unchanged file
    must not cost a hash of the whole archive: matching size *and* mtime is
    taken as proof nothing moved. Only when those differ is the content read.
    """
    entry = rec_mod.SOURCE_BY_KEY.get(source)
    if entry is None:
        return False
    if meta is None:
        meta = _stored_meta(source)
    if not meta:
        return True
    quick = _quick_stat(entry["file"])
    if (quick["size"] == int(meta.get("file_size") or 0)
            and abs(quick["mtime"] - float(meta.get("file_mtime") or 0)) < 1e-6):
        return False
    fp = fingerprint(entry["file"])
    # An empty digest means the file is gone. Do not wipe a populated table
    # over a file that has merely been moved -- that is a fault to report, not
    # a change to apply.
    if not fp["digest"]:
        return False
    return fp["digest"] != (meta.get("digest") or "")


def _json(value) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _json_obj(value) -> str:
    """Serialise a dict, preserving the empty case as {} rather than []."""
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)


RECORD_COLUMNS = (
    "id", "source", "local_id", "contact_id", "emails", "phones", "raw",
    "name", "username", "title",
    "url", "site_url", "location", "organization", "created_at", "activity_at",
    "run", "scraped_country", "scraped_country_code", "scraped_gender",
    "preview", "full_text", "tags", "apply_links", "messaging", "links",
    "ts", "created_ts", "search_blob",
)

CONTACT_COLUMNS = (
    "id", "source", "primary_email", "provider", "emails", "email_count",
    "phones", "primary_phone", "phone_count", "has_whatsapp", "name",
    "username", "title", "url", "site_url", "location", "organization",
    "created_at", "activity_at", "run", "preview", "tags", "apply_links",
    "messaging", "links", "posts", "post_count", "ts", "created_ts",
    "activity_ts", "scraped_country", "scraped_country_code", "scraped_gender",
    "country", "country_code", "gender", "name_key", "email_key",
    "country_key", "search_blob",
)


def _insert_sql(table: str, columns) -> str:
    cols = ", ".join(f"`{c}`" for c in columns)
    marks = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO `{table}` ({cols}) VALUES ({marks})"


def _record_row(r: dict, contact_id: str) -> tuple:
    return (
        r["id"], r["source"], r["local_id"], contact_id, _json(r["emails"]),
        _json(r.get("phones")), _json_obj(r.get("raw")),
        r["name"][:255],
        r["username"][:190], r["title"][:512], r["url"][:768], r["site_url"][:768],
        r["location"][:255], r["organization"][:255], r["created_at"],
        r["activity_at"], r["run"], r["scraped_country"][:96],
        r["scraped_country_code"], r["scraped_gender"][:10], r["preview"],
        r["full"], _json(r["tags"]), _json(r["apply_links"]),
        _json(r["messaging"]), _json(r["links"]), r["ts"], r["created_ts"],
        r["blob"],
    )


def _contact_row(c: dict) -> tuple:
    emails = c["emails"]
    primary = emails[0] if emails else ""
    # Country/gender start as whatever the scraper worked out; `apply_all_
    # enrichment` layers Claude's inference over the top straight after.
    country = c.get("scraped_country") or ""
    code = c.get("scraped_country_code") or ""
    gender = c.get("scraped_gender") or ""
    phones = phone_first(c.get("phones") or [])
    primary_phone = phones[0]["number"] if phones else ""
    has_whatsapp = 1 if any(p.get("whatsapp") for p in phones) else 0
    return (
        c["id"], c["source"], primary, email_provider(primary),
        _json(emails), len(emails),
        _json(phones), primary_phone[:24], len(phones), has_whatsapp,
        c["name"][:255], c["username"][:190], c["title"][:512], c["url"][:768],
        c["site_url"][:768], c["location"][:255], c["organization"][:255],
        c["created_at"], c["activity_at"], c["run"], c["preview"],
        _json(c["tags"]), _json(c["apply_links"]), _json(c["messaging"]),
        _json(c["links"]), _json(c["posts"]), c["post_count"], c["ts"],
        c["created_ts"], c["activity_ts"], country[:96], code, gender[:10],
        country[:96], code, gender[:10],
        db.text_key(c["name"] or c["username"])[:255],
        db.text_key(primary)[:190], db.text_key(country)[:96], c["blob"],
    )


def _purge_source(source: str) -> None:
    """Drop everything ingested for one source. Child tables go first, so a
    failure part-way cannot leave rows pointing at a parent that is gone."""
    db.execute("DELETE ce FROM contact_emails ce JOIN contacts c "
               "ON c.id = ce.contact_id WHERE c.source = %s", (source,))
    db.execute("DELETE cp FROM contact_phones cp JOIN contacts c "
               "ON c.id = cp.contact_id WHERE c.source = %s", (source,))
    db.execute("DELETE FROM records WHERE source = %s", (source,))
    db.execute("DELETE FROM contacts WHERE source = %s", (source,))


def _digest_prefix(path: Path, length: int) -> str:
    """SHA-256 of the first ``length`` bytes of a file ("" if it is shorter)."""
    if length <= 0:
        return ""
    h = hashlib.sha256()
    remaining = length
    try:
        with path.open("rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(_HASH_CHUNK, remaining))
                if not chunk:
                    return ""  # file is shorter than it was: not an append
                h.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _appended_lines(path: Path, offset: int, end: int):
    """The raw JSON objects between byte ``offset`` and byte ``end``.

    Returns None when the tail cannot be read as whole lines -- if the file did
    not previously end on a newline, ``offset`` lands mid-record and the only
    safe move is a full rebuild.

    Reading stops at ``end`` rather than at EOF on purpose: ``end`` is the size
    the fingerprint we are about to store was taken at. A scraper appending
    while we work would otherwise have us ingest past the point that
    fingerprint describes, and the next sync would re-read the difference.
    """
    try:
        with path.open("rb") as fh:
            if offset > 0:
                fh.seek(offset - 1)
                if fh.read(1) != b"\n":
                    return None
            else:
                fh.seek(0)
            tail = fh.read(max(0, end - offset))
    except OSError:
        return None
    out = []
    for line in tail.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # A half-written last line is normal while a scraper is running;
            # it will be complete (and picked up) on the next sync.
            continue
    return out


def _sync_appended(entry: dict, meta: dict, fp: dict, started: float,
                   verbose: bool) -> dict | None:
    """Ingest only the lines a scraper appended since the last sync.

    Returns None when an incremental pass is not safe, so the caller falls back
    to a full rebuild. This is what lets the scrape watchdog refresh the UI
    every couple of seconds: adding fifty profiles costs fifty inserts instead
    of re-reading and re-merging a 7MB archive.
    """
    source = entry["key"]
    if not entry.get("jsonl"):
        return None
    offset = int(meta.get("file_size") or 0)
    if offset <= 0 or fp["size"] < offset:
        return None
    # The file must still *start* with exactly what we ingested last time.
    if _digest_prefix(entry["file"], offset) != (meta.get("digest") or ""):
        return None

    added = _appended_lines(entry["file"], offset, fp["size"])
    if added is None:
        return None
    if not added:
        # The file grew by whitespace, or by a line that is still being
        # written. Nothing to do, and no fingerprint to advance.
        return {"ok": True, "source": source, "skipped": True, "mode": "append",
                "records": int(meta.get("record_count") or 0),
                "contacts": int(meta.get("contact_count") or 0), "seconds": 0.0}

    base = int(meta.get("record_count") or 0)
    counts = ingest_records(source, added, first_index=base + 1)
    total_records, total_contacts = counts["records"], counts["contacts"]
    with db.transaction():
        _write_sync_meta(source, entry["file"], fp, total_records, total_contacts)

    elapsed = time.time() - started
    if verbose:
        print(f"[sync] {source}: +{counts['added']} appended records "
              f"({total_contacts} contacts, {elapsed:.1f}s)")
    return {"ok": True, "source": source, "skipped": False, "mode": "append",
            "records": total_records, "contacts": total_contacts,
            "added": counts["added"], "seconds": round(elapsed, 2)}


def _touched_contacts(source: str, new_recs: list[dict]) -> list[str]:
    """Which stored contacts these records can join, bridge, or replace.

    There are three ways a new record reaches one, and all three have to be
    asked -- the rebuild below deletes a contact before re-inserting it, so a
    contact it should have claimed and did not is one whose primary key is
    still taken when the insert comes round:

      * a shared **address** -- the common case;
      * a shared **number** -- ``merge_by_email`` links on those too, so a
        person reachable only by WhatsApp has to be found the same way, and two
        rows that are really one person collapse instead of doubling up;
      * the record's **own id**, for a record a scraper re-emits. The contact
        it currently belongs to may share neither an address nor a number with
        the new version (they changed, or there never were any), and a contact
        id *is* a record id -- so missing this one is the case that collides.
    """
    ids: set[str] = set()

    def lookup(sql: str, values: list) -> None:
        for i in range(0, len(values), 500):
            chunk = values[i:i + 500]
            marks = ", ".join(["%s"] * len(chunk))
            ids.update(
                row["contact_id"]
                for row in db.query(sql.format(marks=marks), [source] + chunk)
                if row["contact_id"])

    emails = sorted({e for r in new_recs for e in r["emails"]})
    if emails:
        lookup("SELECT DISTINCT ce.contact_id FROM contact_emails ce "
               "JOIN contacts c ON c.id = ce.contact_id "
               "WHERE c.source = %s AND ce.email IN ({marks})", emails)

    phones = sorted({p["number"] for r in new_recs
                     for p in (r.get("phones") or []) if p.get("number")})
    if phones:
        lookup("SELECT DISTINCT cp.contact_id FROM contact_phones cp "
               "JOIN contacts c ON c.id = cp.contact_id "
               "WHERE c.source = %s AND cp.phone IN ({marks})", phones)

    lookup("SELECT DISTINCT r.contact_id FROM records r "
           "WHERE r.source = %s AND r.id IN ({marks})",
           sorted({r["id"] for r in new_recs}))
    return sorted(ids)


def ingest_records(source: str, raws: list, first_index: int = 1) -> dict:
    """Store freshly scraped records and fold them into the merged contacts.

    This is the single write path into the archive: the scrapers call it as
    each record is collected, and the file importer calls it with a batch. It
    is incremental by construction -- only the contacts these records touch are
    rebuilt, so the cost is proportional to what arrived, not to what is
    already stored.

    ``raws`` are the scraper's own dicts; normalising them here (rather than
    asking each scraper to) keeps one definition of what a record is.
    """
    entry = rec_mod.SOURCE_BY_KEY.get(source)
    if entry is None:
        raise ValueError(f"unknown source: {source}")
    if not raws:
        return {"added": 0,
                "records": source_record_count(source),
                "contacts": source_contact_count(source)}

    build = entry["build"]
    new_recs = [build(d, first_index + i) for i, d in enumerate(raws)]

    # A new record joins an existing contact when they share an address or a
    # number, and it can even bridge two contacts that were separate until now.
    # So the set to re-merge is the new records plus every record of every
    # contact they touch -- and no further, because within a source an
    # identifier belongs to exactly one contact already.
    touched_ids = _touched_contacts(source, new_recs)

    # Rebuild those contacts from the records already stored, so the merge sees
    # the same inputs a full rebuild would.
    existing: list[dict] = []
    for i in range(0, len(touched_ids), 500):
        chunk = touched_ids[i:i + 500]
        marks = ", ".join(["%s"] * len(chunk))
        existing += [_record_from_row(row) for row in db.query(
            f"SELECT * FROM records WHERE contact_id IN ({marks})", chunk)]

    # New records can also collide with ids already present (a scraper that
    # re-emits a profile it has seen). The later line wins, as it does on disk.
    by_id = {r["id"]: r for r in existing}
    for r in new_recs:
        by_id[r["id"]] = r
    merged = rec_mod.merge_by_email(list(by_id.values()))

    contact_of = {m: c["id"] for c in merged for m in c["member_ids"]}
    replaced_records = sorted(by_id.keys())

    with db.bulk_load(), db.transaction():
        for i in range(0, len(replaced_records), 500):
            chunk = replaced_records[i:i + 500]
            marks = ", ".join(["%s"] * len(chunk))
            db.execute(f"DELETE FROM records WHERE id IN ({marks})", chunk)
        for i in range(0, len(touched_ids), 500):
            chunk = touched_ids[i:i + 500]
            marks = ", ".join(["%s"] * len(chunk))
            db.execute(f"DELETE FROM contact_emails WHERE contact_id IN ({marks})",
                       chunk)
            db.execute(f"DELETE FROM contact_phones WHERE contact_id IN ({marks})",
                       chunk)
            db.execute(f"DELETE FROM contacts WHERE id IN ({marks})", chunk)

        db.insert_chunked(
            _insert_sql("records", RECORD_COLUMNS),
            (_record_row(r, contact_of.get(r["id"], "")) for r in by_id.values()),
        )
        db.insert_chunked(
            _insert_sql("contacts", CONTACT_COLUMNS),
            (_contact_row(c) for c in merged),
        )
        db.insert_chunked(
            "INSERT INTO contact_emails (contact_id, email, pos) VALUES (%s, %s, %s)",
            ((c["id"], e, i) for c in merged for i, e in enumerate(c["emails"])),
        )
        db.insert_chunked(
            "INSERT INTO contact_phones (contact_id, phone, whatsapp, via, pos) "
            "VALUES (%s, %s, %s, %s, %s)",
            ((c["id"], p["number"], 1 if p.get("whatsapp") else 0,
              (p.get("via") or "")[:16], i)
             for c in merged for i, p in enumerate(phone_first(c.get("phones") or []))),
        )
        total_records = source_record_count(source)
        total_contacts = source_contact_count(source)

    _reapply_for_contacts([c["id"] for c in merged])
    return {"added": len(new_recs), "records": total_records,
            "contacts": total_contacts}


def rebuild_contacts(source: str = "", verbose: bool = True) -> dict:
    """Rebuild the merged contacts for a source from ``records``.

    ``contacts`` is a materialised view of the merge, so it can always be
    thrown away and recomputed from the records themselves -- no files, no
    network. That is what makes it safe for a schema migration to drop it, and
    it is the repair path if the merge is ever suspected of being wrong.
    """
    sources = [source] if source else [s["key"] for s in rec_mod.STORED_SOURCES]
    out = {}
    for key in sources:
        rows = db.query("SELECT * FROM records WHERE source = %s", (key,))
        if not rows:
            out[key] = 0
            continue
        recs = [_record_from_row(r) for r in rows]
        merged = rec_mod.merge_by_email(recs)
        contact_of = {m: c["id"] for c in merged for m in c["member_ids"]}

        with db.bulk_load(), db.transaction():
            db.execute("DELETE cp FROM contact_phones cp JOIN contacts c "
                       "ON c.id = cp.contact_id WHERE c.source = %s", (key,))
            db.execute("DELETE ce FROM contact_emails ce JOIN contacts c "
                       "ON c.id = ce.contact_id WHERE c.source = %s", (key,))
            db.execute("DELETE FROM contacts WHERE source = %s", (key,))
            db.insert_chunked(
                _insert_sql("contacts", CONTACT_COLUMNS),
                (_contact_row(c) for c in merged))
            db.insert_chunked(
                "INSERT INTO contact_emails (contact_id, email, pos) "
                "VALUES (%s, %s, %s)",
                ((c["id"], e, i) for c in merged
                 for i, e in enumerate(c["emails"])))
            db.insert_chunked(
                "INSERT INTO contact_phones (contact_id, phone, whatsapp, via, pos) "
                "VALUES (%s, %s, %s, %s, %s)",
                ((c["id"], p["number"], 1 if p.get("whatsapp") else 0,
                  (p.get("via") or "")[:16], i)
                 for c in merged
                 for i, p in enumerate(phone_first(c.get("phones") or []))))
            # records.contact_id points at the contact each record merged into;
            # the ids can change when the merge is recomputed.
            db.insert_chunked(
                "UPDATE records SET contact_id = %s WHERE id = %s",
                [(cid, rid) for rid, cid in contact_of.items()])

        db.apply_all_enrichment(key)
        db.refresh_all_sent(key)
        out[key] = len(merged)
        if verbose:
            print(f"[rebuild] {key}: {len(rows)} records -> {len(merged)} contacts")
    if not source:
        # Every source has just been recomputed, which is exactly what the flag
        # asks for. Clearing it here means any entry point can satisfy it by
        # calling this, rather than each having to remember the second half.
        db.clear_contacts_rebuild_flag()
    return out


def ensure_contacts_rebuilt(verbose: bool = True) -> bool:
    """Recompute the merged contacts if a schema migration dropped them.

    ``contacts`` is a materialised view of ``records``, so a migration is free
    to drop it -- but only the merge can put it back, and ``db.bootstrap`` (the
    thing that migrates) cannot call the merge without importing this module
    into its own importer. So the flag is left for whoever boots next, and
    every entry point that boots the database asks this: the server, the
    scrapers through ``RecordStore``, and the phone tools. Without it, being
    the first to run after a version bump means serving an empty archive.
    """
    if not db.contacts_need_rebuild():
        return False
    if verbose:
        print("[db] rebuilding merged contacts after a schema change",
              file=sys.stderr)
    rebuild_contacts(verbose=verbose)
    return True


def source_record_count(source: str) -> int:
    return int(db.scalar("SELECT COUNT(*) AS n FROM records WHERE source = %s",
                         (source,), default=0))


def source_contact_count(source: str) -> int:
    return int(db.scalar("SELECT COUNT(*) AS n FROM contacts WHERE source = %s",
                         (source,), default=0))


def done_local_ids(source: str) -> set:
    """Which of a source's records are already stored, by the scraper's own key.

    ``records.local_id`` is exactly the key each scraper de-duplicates on -- a
    GitHub login, an about.me username, a Discourse post id -- so this is the
    database's answer to what `load_done_keys` used to read out of the file.
    """
    return {r["local_id"] for r in db.query(
        "SELECT local_id FROM records WHERE source = %s", (source,))}


def relabel_runs(source: str, from_runs: set, into: int) -> int:
    """Move every record on ``from_runs`` onto run ``into``.

    A step is only a label, so this is an UPDATE plus a refresh of the merged
    rows that carry the label. (It used to mean rewriting the whole JSONL file
    and re-ingesting it, with a .bak alongside in case that went wrong.)
    """
    if not from_runs:
        return 0
    marks = ", ".join(["%s"] * len(from_runs))
    args = [into, source] + sorted(from_runs)
    with db.transaction():
        moved = db.execute(
            f"UPDATE records SET run = %s WHERE source = %s AND run IN ({marks})",
            args)
        # `contacts.run` is the representative record's run, and the merge takes
        # the newest record as representative -- so recompute it the same way
        # rather than assuming every member moved.
        db.execute(
            f"UPDATE contacts c JOIN records r ON r.id = c.id "
            f"SET c.run = r.run WHERE c.source = %s", (source,))
    return moved


def runs_present(source: str) -> set:
    return {int(r["run"]) for r in db.query(
        "SELECT DISTINCT run FROM records WHERE source = %s", (source,))}


def next_run(source: str) -> int:
    """One past the highest run already stored, so runs keep counting up.

    Records written before runs were numbered have no run and count as 1, which
    is what the old file-based version did -- the first scrape after that change
    is run 2 rather than restarting on top of them.
    """
    return int(db.scalar(
        "SELECT COALESCE(MAX(GREATEST(run, 1)), 0) AS n FROM records "
        "WHERE source = %s", (source,), default=0)) + 1


def _record_from_row(row: dict) -> dict:
    """Turn a stored record row back into the in-memory shape the merge wants."""
    def jload(value):
        try:
            return json.loads(value or "[]")
        except (ValueError, TypeError):
            return []

    return {
        "id": row["id"], "source": row["source"], "local_id": row["local_id"],
        "emails": jload(row["emails"]),
        "phones": jload(row.get("phones")),
        "raw": jload(row.get("raw")) or {},
        "name": row["name"], "username": row["username"],
        "title": row["title"], "url": row["url"], "site_url": row["site_url"],
        "location": row["location"], "organization": row["organization"],
        "created_at": row["created_at"], "activity_at": row["activity_at"],
        "run": row["run"], "scraped_country": row["scraped_country"],
        "scraped_country_code": row["scraped_country_code"],
        "scraped_gender": row["scraped_gender"], "preview": row["preview"] or "",
        "full": row["full_text"] or "", "tags": jload(row["tags"]),
        "apply_links": jload(row["apply_links"]),
        "messaging": jload(row["messaging"]), "links": jload(row["links"]),
        "ts": float(row["ts"] or 0), "created_ts": float(row["created_ts"] or 0),
        "activity_ts": rec_mod.parse_ts(row["activity_at"] or ""),
        "blob": row["search_blob"] or "",
    }


def _reapply_for_contacts(contact_ids: list[str]) -> None:
    """Re-layer enrichment and sent counts onto contacts that were just rebuilt."""
    if not contact_ids:
        return
    for i in range(0, len(contact_ids), 500):
        chunk = contact_ids[i:i + 500]
        marks = ", ".join(["%s"] * len(chunk))
        db.execute(
            f"UPDATE contacts c JOIN enrichment e ON e.email = c.primary_email SET "
            f"  c.country = COALESCE(NULLIF(e.country, ''), c.scraped_country, ''), "
            f"  c.country_code = COALESCE(NULLIF(e.country_code, ''), "
            f"                            c.scraped_country_code, ''), "
            f"  c.gender = COALESCE(NULLIF(e.gender, ''), c.scraped_gender, ''), "
            f"  c.country_key = LOWER(COALESCE(NULLIF(e.country, ''), "
            f"                                 c.scraped_country, '')) "
            f"WHERE c.id IN ({marks})", chunk)
    db.refresh_sent_for_contacts(contact_ids)


def _write_sync_meta(source: str, path: Path, fp: dict, records: int,
                     contacts: int) -> None:
    db.execute(
        "INSERT INTO sync_meta (source, file_path, file_size, file_mtime, "
        "  digest, record_count, contact_count, synced_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE file_path = VALUES(file_path), "
        "  file_size = VALUES(file_size), file_mtime = VALUES(file_mtime), "
        "  digest = VALUES(digest), record_count = VALUES(record_count), "
        "  contact_count = VALUES(contact_count), synced_at = NOW()",
        (source, str(path), fp["size"], fp["mtime"], fp["digest"],
         records, contacts),
    )


def sync_source(source: str, force: bool = False, verbose: bool = True) -> dict:
    """Bring one source's tables in line with its file.

    Skips entirely when the file is unchanged, ingests just the new lines when
    it has only been appended to, and rebuilds the source from scratch when it
    has been rewritten (which is what ``merge_runs`` does to it).
    """
    entry = rec_mod.SOURCE_BY_KEY.get(source)
    if entry is None:
        return {"ok": False, "source": source, "error": "unknown source"}
    with _lock_for(source):
        return _sync_source_locked(entry, source, force, verbose)


def _sync_source_locked(entry: dict, source: str, force: bool,
                        verbose: bool) -> dict:
    started = time.time()
    meta = _stored_meta(source)
    if not force and not is_stale(source, meta):
        return {"ok": True, "source": source, "skipped": True, "mode": "skip",
                "records": int(meta.get("record_count") or 0),
                "contacts": int(meta.get("contact_count") or 0),
                "seconds": 0.0}
    # Only now is the full content hash worth paying for.
    fp = fingerprint(entry["file"])

    if not force and meta:
        incremental = _sync_appended(entry, meta, fp, started, verbose)
        if incremental is not None:
            return incremental

    raw = entry["loader"]()
    contacts = rec_mod.merge_by_email(raw)

    # Which contact each record was merged into -- this is what lets the detail
    # page pull every occurrence's full text back out with one indexed lookup.
    contact_of: dict[str, str] = {}
    for c in contacts:
        for member in c["member_ids"]:
            contact_of[member] = c["id"]

    with db.bulk_load(), db.transaction():
        _purge_source(source)
        db.insert_chunked(
            _insert_sql("records", RECORD_COLUMNS),
            (_record_row(r, contact_of.get(r["id"], "")) for r in raw),
        )
        db.insert_chunked(
            _insert_sql("contacts", CONTACT_COLUMNS),
            (_contact_row(c) for c in contacts),
        )
        db.insert_chunked(
            "INSERT INTO contact_emails (contact_id, email, pos) VALUES (%s, %s, %s)",
            ((c["id"], e, i) for c in contacts for i, e in enumerate(c["emails"])),
        )
        db.insert_chunked(
            "INSERT INTO contact_phones (contact_id, phone, whatsapp, via, pos) "
            "VALUES (%s, %s, %s, %s, %s)",
            ((c["id"], p["number"], 1 if p.get("whatsapp") else 0,
              (p.get("via") or "")[:16], i)
             for c in contacts for i, p in enumerate(phone_first(c.get("phones") or []))),
        )
        _write_sync_meta(source, entry["file"], fp, len(raw), len(contacts))

    # The rebuild wrote scraper-supplied country/gender and zeroed the messaged
    # columns; both live in tables the rebuild never touched, so re-apply them.
    db.apply_all_enrichment(source)
    db.refresh_all_sent(source)

    elapsed = time.time() - started
    if verbose:
        print(f"[sync] {source}: {len(raw)} records -> {len(contacts)} contacts "
              f"({elapsed:.1f}s)")
    return {"ok": True, "source": source, "skipped": False, "mode": "rebuild",
            "records": len(raw), "contacts": len(contacts),
            "seconds": round(elapsed, 2)}


def import_files(force: bool = False, verbose: bool = True) -> dict:
    """Import any legacy scraper files still on disk.

    The scrapers write to the database directly, so this exists for one
    situation: an archive collected before that change, sitting in JSONL files
    that nothing has read into MySQL yet. Sources whose file is absent are
    skipped silently -- that is the normal, fully-migrated state.
    """
    present = [s for s in rec_mod.STORED_SOURCES if s["file"].exists()]
    if not present:
        if verbose:
            print("[import] no scraper files on disk -- nothing to import")
        return {"ok": True, "sources": [], "changed": []}
    results = [sync_source(s["key"], force=force, verbose=verbose)
               for s in present]
    changed = [r for r in results if not r.get("skipped")]
    if verbose and not changed:
        print("[import] scraper files already imported")
    return {"ok": all(r.get("ok") for r in results), "sources": results,
            "changed": [r["source"] for r in changed]}


# Kept under the old name so anything still calling it keeps working.
sync_all = import_files


def sync_status() -> list[dict]:
    """Per-source contents, for the Database page.

    Counts come from the tables rather than from ``sync_meta``, because the
    scrapers write records directly and never touch that table -- it only
    records what a legacy file import last read. ``legacy_file`` flags the one
    situation worth showing: a data file still sitting on disk, waiting to be
    imported.
    """
    out = []
    for s in rec_mod.STORED_SOURCES:
        meta = _stored_meta(s["key"])
        has_file = s["file"].exists()
        out.append({
            "source": s["key"],
            "label": s["label"],
            "records": source_record_count(s["key"]),
            "contacts": source_contact_count(s["key"]),
            "runs": len(runs_present(s["key"]) - {0}),
            "listed": s["key"] in rec_mod.LISTED_KEYS,
            "legacy_file": str(s["file"]) if has_file else "",
            "legacy_pending": has_file and is_stale(s["key"], meta),
        })
    return out


# ---- one-time import of the pre-database JSON state -----------------------
# These three files *were* the storage for everything the server owns. They are
# read once, when their table is still empty, and then left alone -- the copy
# on disk stays as a backup, but the database is the live one from then on.
LEGACY_ENRICH_FILE = rec_mod.SCRAPERS_DIR / "enrich_cache.json"
LEGACY_SENT_FILE = rec_mod.SCRAPERS_DIR / "sent.json"
LEGACY_STATE_FILE = rec_mod.SCRAPERS_DIR / "state.json"
# GitHub's list of logins already looked at and ruled out. Worth carrying over
# rather than rediscovering: each entry stands for several API calls that a
# later run would otherwise spend to reach the same verdict.
LEGACY_SKIPPED_FILE = rec_mod.SCRAPERS_DIR / "github" / "skipped.jsonl"


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def migrate_legacy_json(verbose: bool = True) -> dict:
    """Move enrich_cache.json / sent.json / state.json into their tables.

    Only runs against an empty table, so it cannot overwrite newer values with
    a stale file, and re-running it is a no-op.
    """
    moved = {"enrichment": 0, "sent_log": 0, "skipped": 0, "app_state": 0}

    if db.enrichment_count() == 0:
        rows = []
        for email, enr in _read_json_object(LEGACY_ENRICH_FILE).items():
            if not isinstance(enr, dict):
                continue
            rows.append((
                (email or "").strip().lower()[:190],
                (enr.get("country") or "").strip()[:96],
                (enr.get("country_code") or "").strip().upper()[:2],
                (enr.get("gender") or "").strip().lower()[:10],
            ))
        rows = [r for r in rows if r[0]]
        if rows:
            moved["enrichment"] = db.insert_chunked(
                "INSERT IGNORE INTO enrichment (email, country, country_code, "
                "gender) VALUES (%s, %s, %s, %s)", rows)

    if db.sent_count() == 0:
        rows = []
        for email, ent in _read_json_object(LEGACY_SENT_FILE).items():
            if not isinstance(ent, dict):
                continue
            rows.append((
                (email or "").strip().lower()[:190],
                int(ent.get("count") or 0),
                str(ent.get("last_sent") or "")[:40],
                str(ent.get("last_subject") or "")[:512],
                1 if ent.get("manual") else 0,
            ))
        rows = [r for r in rows if r[0]]
        if rows:
            moved["sent_log"] = db.insert_chunked(
                "INSERT IGNORE INTO sent_log (email, send_count, last_sent, "
                "last_subject, `manual`) VALUES (%s, %s, %s, %s, %s)", rows)

    if int(db.scalar("SELECT COUNT(*) AS n FROM skipped WHERE source = 'github'",
                     default=0)) == 0:
        rows = []
        for d in rec_mod.read_jsonl(LEGACY_SKIPPED_FILE):
            login = str(d.get("login") or "").strip()
            if login:
                rows.append(("github", login[:190],
                             str(d.get("reason") or "")[:64]))
        if rows:
            moved["skipped"] = db.insert_chunked(
                "INSERT IGNORE INTO skipped (source, local_id, reason) "
                "VALUES (%s, %s, %s)", rows)

    if int(db.scalar("SELECT COUNT(*) AS n FROM app_state", default=0)) == 0:
        state = _read_json_object(LEGACY_STATE_FILE)
        for key, value in state.items():
            db.state_put(str(key)[:64], value)
        moved["app_state"] = len(state)

    if verbose and any(moved.values()):
        print(f"[sync] imported legacy JSON state: "
              f"{moved['enrichment']} enrichments, {moved['sent_log']} sent "
              f"entries, {moved['skipped']} ruled-out logins, "
              f"{moved['app_state']} state keys")
    return moved


def main(argv: list[str]) -> int:
    force = "--force" in argv or "-f" in argv
    wanted = [a for a in argv if not a.startswith("-")]
    db.bootstrap()
    migrate_legacy_json()
    if wanted:
        for key in wanted:
            result = sync_source(key, force=force)
            if not result.get("ok"):
                print(f"[sync] {key}: {result.get('error')}", file=sys.stderr)
                return 1
    else:
        sync_all(force=force)
    counts = db.table_counts()
    print(f"[sync] database now holds {counts['contacts']} contacts "
          f"from {counts['records']} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
