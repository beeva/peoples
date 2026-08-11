#!/usr/bin/env python3
"""Find phone / WhatsApp numbers in the archive, and keep them up to date.

Two jobs, deliberately separate because they cost wildly different amounts:

``backfill`` re-reads every record's stored ``raw`` and re-extracts phones from
it. No network, no rate limits, seconds to run. It exists because ``records.raw``
keeps each scraper's original JSON, so adding a new kind of extraction does not
mean re-scraping anything -- the text is already here.

``scrape`` is the expensive one: it revisits GitHub users' portfolio sites and
profile READMEs, which is where a developer's number actually tends to be
(almost never in the GitHub profile itself). That is thousands of requests to
third-party sites, so it is never run automatically -- see ``npm run
scrape:phones``.

    python dbphones.py backfill          # re-extract from stored records
    python dbphones.py backfill github   # one source
    python dbphones.py status            # what has been found so far
"""
import json
import sys
from pathlib import Path

# `common` lives under scrapers/, and this runs from the project root. Said
# here rather than relied on as a side effect of importing `records`, which
# also does it -- an import reordered by a tool would otherwise break the CLI.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scrapers"))

import db  # noqa: E402
import dbsync  # noqa: E402
import records as rec_mod  # noqa: E402
from common.phones import (  # noqa: E402
    as_entry,
    extract_phones,
    merge_phones,
    phone_first,
)


def _texts_of(source: str, raw: dict) -> str:
    """The parts of a stored record worth scanning for a number.

    Scanning the whole JSON blob would work, but it also drags in ids,
    timestamps and follower counts -- exactly the digit soup the extractor is
    built to resist. Naming the free-text fields keeps the input clean.
    """
    if source == "discourse":
        return raw.get("cooked") or ""
    if source == "aboutme":
        links = raw.get("links") or []
        flat = " ".join(
            str(it.get("url") or it.get("href") or "") if isinstance(it, dict)
            else str(it) for it in links)
        return " ".join([raw.get("summary") or "", flat,
                         " ".join(raw.get("roles") or [])])
    if source == "devto":
        contact = raw.get("contact") or {}
        return " ".join([raw.get("description") or "",
                         " ".join(contact.get("messaging") or []),
                         " ".join(contact.get("apply_links") or [])])
    if source == "github":
        return " ".join(filter(None, (raw.get("bio"), raw.get("site_url"),
                                      raw.get("site_text"))))
    return ""


def backfill(source: str = "", *, verbose: bool = True) -> dict:
    """Re-extract phones from stored records and fold them into contacts.

    Records whose extraction result is unchanged are skipped, so re-running is
    cheap and only the contacts that actually gained a number get rebuilt.
    """
    sources = [source] if source else [s["key"] for s in rec_mod.STORED_SOURCES]
    summary = {}
    for key in sources:
        rows = db.query(
            "SELECT id, phones, raw FROM records WHERE source = %s", (key,))
        updates = []
        for row in rows:
            try:
                raw = json.loads(row["raw"] or "{}")
            except ValueError:
                continue
            try:
                before = json.loads(row["phones"] or "[]")
            except ValueError:
                before = []
            # Anything a scraper stored on the record itself (the phone pass
            # writes there) is kept and merged with what the text yields.
            found = phone_first(merge_phones(raw.get("phones") or [],
                                             extract_phones(_texts_of(key, raw))))
            if found != before:
                updates.append((json.dumps(found, ensure_ascii=False), row["id"]))

        if updates:
            with db.transaction():
                db.execute_many("UPDATE records SET phones = %s WHERE id = %s",
                                updates)
        touched = _rebuild_contact_phones(key)
        with_phone = int(db.scalar(
            "SELECT COUNT(*) AS n FROM contacts WHERE source = %s "
            "AND phone_count > 0", (key,), default=0))
        with_wa = int(db.scalar(
            "SELECT COUNT(*) AS n FROM contacts WHERE source = %s "
            "AND has_whatsapp = 1", (key,), default=0))
        summary[key] = {"records_updated": len(updates), "contacts": touched,
                        "with_phone": with_phone, "with_whatsapp": with_wa}
        if verbose:
            print(f"[phones] {key}: {len(updates)} records updated -> "
                  f"{with_phone} contacts with a number ({with_wa} on WhatsApp)")
    return summary


def _rebuild_contact_phones(source: str) -> int:
    """Recompute every contact's phone list from its member records.

    Done in bulk rather than through the merge, because nothing else about the
    contacts has changed -- only the numbers hanging off them.
    """
    rows = db.query(
        "SELECT contact_id, phones FROM records "
        "WHERE source = %s AND contact_id <> '' AND phones IS NOT NULL "
        "AND phones <> '[]'", (source,))
    by_contact: dict = {}
    for r in rows:
        try:
            phones = json.loads(r["phones"] or "[]")
        except ValueError:
            continue
        if phones:
            by_contact.setdefault(r["contact_id"], []).append(phones)

    merged = {cid: phone_first(merge_phones(*groups))
              for cid, groups in by_contact.items()}

    with db.transaction():
        # Clear first: a number that has stopped matching must disappear, not
        # linger because nothing overwrote it.
        db.execute(
            "UPDATE contacts SET phones = '[]', primary_phone = '', "
            "phone_count = 0, has_whatsapp = 0 WHERE source = %s", (source,))
        db.execute(
            "DELETE cp FROM contact_phones cp JOIN contacts c "
            "ON c.id = cp.contact_id WHERE c.source = %s", (source,))
        if merged:
            db.insert_chunked(
                "UPDATE contacts SET phones = %s, primary_phone = %s, "
                "phone_count = %s, has_whatsapp = %s WHERE id = %s",
                [(json.dumps(ps, ensure_ascii=False), ps[0]["number"][:24],
                  len(ps), 1 if any(p.get("whatsapp") for p in ps) else 0, cid)
                 for cid, ps in merged.items() if ps])
            db.insert_chunked(
                "INSERT INTO contact_phones (contact_id, phone, whatsapp, via, pos) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(cid, p["number"], 1 if p.get("whatsapp") else 0,
                  (p.get("via") or "")[:16], i)
                 for cid, ps in merged.items() for i, p in enumerate(ps)])
    return len(merged)


# How many different people in one source may hold the same number before it
# stops being anyone's. The merge collapses contacts that share a number, so
# after a rebuild a real number belongs to exactly one contact; a handful of
# genuine duplicates can survive a bulk phone update that did not re-merge.
# Well past that, what is being shared is not a number but a page: a template
# footer, an embedded widget, a stylesheet served to every site alike.
SHARED_NUMBER_LIMIT = 3


def _boilerplate_numbers(source: str) -> set:
    """Numbers held by so many contacts that they cannot belong to a person."""
    return {r["phone"] for r in db.query(
        "SELECT cp.phone AS phone FROM contact_phones cp "
        "JOIN contacts c ON c.id = cp.contact_id WHERE c.source = %s "
        "GROUP BY cp.phone HAVING COUNT(DISTINCT cp.contact_id) > %s",
        (source, SHARED_NUMBER_LIMIT))}


def revalidate(source: str = "", *, verbose: bool = True) -> dict:
    """Re-check every stored number against the current validation rules.

    Extraction gets stricter over time -- a rule added after a pass ran (say,
    "a national number does not keep its trunk prefix") leaves already-stored
    numbers that would no longer be accepted. Rather than re-crawl thousands of
    sites to find that out, the stored numbers are simply re-run through the
    validator; anything that no longer passes is dropped.

    Two rules, because they catch different failures. The validator judges a
    number on its own and cannot see that ``+20162017`` -- a stylesheet's
    ``unicode-range: U+2016-2017`` read as an Egyptian number -- is not one:
    it is perfectly well-formed. What gives it away is that it turned up on
    thirteen unrelated people, and that only the whole archive can show. So a
    number is dropped either because it no longer validates, or because too
    many contacts hold it for it to be anybody's.
    """
    sources = [source] if source else [s["key"] for s in rec_mod.STORED_SOURCES]
    summary = {}
    for key in sources:
        shared = _boilerplate_numbers(key)
        rows = db.query("SELECT id, phones, raw FROM records WHERE source = %s "
                        "AND phones IS NOT NULL AND phones <> '[]'", (key,))
        updates, dropped = [], 0
        for row in rows:
            try:
                phones = json.loads(row["phones"] or "[]")
            except ValueError:
                continue
            kept = []
            for p in phones:
                # as_entry re-runs the validator, and returns None for an entry
                # that no longer passes it (or was never well-formed).
                entry = as_entry(p)
                if entry and entry["number"] not in shared:
                    kept.append(entry)
                else:
                    dropped += 1
            if len(kept) != len(phones):
                blob = json.dumps(kept, ensure_ascii=False)
                try:
                    raw = json.loads(row["raw"] or "{}")
                except ValueError:
                    raw = {}
                raw["phones"] = kept
                updates.append((blob, json.dumps(raw, ensure_ascii=False), row["id"]))

        if updates:
            with db.transaction():
                db.execute_many(
                    "UPDATE records SET phones = %s, raw = %s WHERE id = %s",
                    updates)
            _rebuild_contact_phones(key)
        summary[key] = {"records_changed": len(updates), "numbers_dropped": dropped}
        if verbose and dropped:
            print(f"[phones] {key}: dropped {dropped} number(s) -- invalid, or "
                  f"held by more than {SHARED_NUMBER_LIMIT} contacts -- across "
                  f"{len(updates)} record(s)")
    return summary


def status() -> dict:
    rows = db.query(
        "SELECT source, COUNT(*) AS n, "
        "       SUM(phone_count > 0) AS with_phone, "
        "       SUM(has_whatsapp = 1) AS with_whatsapp "
        "FROM contacts GROUP BY source")
    return {r["source"]: {"contacts": int(r["n"]),
                          "with_phone": int(r["with_phone"] or 0),
                          "with_whatsapp": int(r["with_whatsapp"] or 0)}
            for r in rows}


def main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    db.bootstrap(verbose=False)
    # Every one of these reads or rewrites the merged contacts, so they have to
    # exist first -- a schema bump drops them and leaves them for whoever boots.
    dbsync.ensure_contacts_rebuilt()
    if cmd == "backfill":
        backfill(argv[1] if len(argv) > 1 else "")
        return 0
    if cmd == "revalidate":
        revalidate(argv[1] if len(argv) > 1 else "")
        for key, st in sorted(status().items()):
            print(f"  {key:<10} {st['with_phone']:>5} with a number  "
                  f"{st['with_whatsapp']:>5} on WhatsApp")
        return 0
    if cmd == "status":
        for key, st in sorted(status().items()):
            print(f"  {key:<10} {st['contacts']:>6} contacts  "
                  f"{st['with_phone']:>5} with a number  "
                  f"{st['with_whatsapp']:>5} on WhatsApp")
        return 0
    print(f"unknown command '{cmd}' (expected backfill, revalidate or status)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
