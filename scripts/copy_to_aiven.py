#!/usr/bin/env python3
"""Copy every table from the local database into the managed one, row by row.

The obvious tool for this is `mysqldump | mysql`, and on this project it does
not work: XAMPP ships MariaDB 10.4's client binaries, while managed MySQL 8
authenticates with `caching_sha2_password` -- a plugin MariaDB's client does
not carry. It fails at the handshake with

    ERROR 1045 (28000): Plugin caching_sha2_password could not be loaded

before a single statement runs. PyMySQL speaks that auth natively and is
already the project's only dependency, so the copy goes through it instead.
Moving rows as Python objects also sidesteps SQL dialect, quoting, and reserved
words entirely -- there is no generated SQL text for the far side to re-parse.

Reads stream (SSDictCursor), so a large `records` table never lands in memory
all at once.

    python scripts/copy_to_aiven.py                 # local 3307 -> whatever .env names
    python scripts/copy_to_aiven.py --truncate      # empty the target tables first

The source is always the local server; the destination is whatever MYSQL_* in
`.env` points at. Run it with `.env` already pointing at the managed database.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor, SSDictCursor

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
import server  # noqa: E402,F401 -- imported for its side effect: loads .env

# Order is cosmetic (the schema declares no foreign keys); records and contacts
# lead so the counts that matter appear first.
TABLES = ["records", "contacts", "contact_emails", "contact_phones", "skipped",
          "slack_users", "enrichment", "sent_log", "app_state", "sync_meta"]

# `records` carries a MEDIUMTEXT `raw` per row, so it moves in smaller batches
# to keep each INSERT comfortably under the server's max_allowed_packet.
BATCH = {"records": 200, "slack_users": 500}
DEFAULT_BATCH = 1000

LOCAL_HOST = os.environ.get("LOCAL_MYSQL_HOST", "127.0.0.1")
LOCAL_PORT = int(os.environ.get("LOCAL_MYSQL_PORT", "3307"))
LOCAL_USER = os.environ.get("LOCAL_MYSQL_USER", "root")
LOCAL_PASSWORD = os.environ.get("LOCAL_MYSQL_PASSWORD", "")
DB_NAME = os.environ.get("MYSQL_DATABASE", "email_scrapper")


def _source():
    return pymysql.connect(host=LOCAL_HOST, port=LOCAL_PORT, user=LOCAL_USER,
                           password=LOCAL_PASSWORD, database=DB_NAME,
                           charset="utf8mb4", cursorclass=SSDictCursor)


def _target(autocommit=True):
    ssl = {} if os.environ.get("MYSQL_SSL", "").strip() == "1" else None
    ca = os.environ.get("MYSQL_SSL_CA", "").strip()
    if ca:
        ssl = {"ca": ca}
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.environ["MYSQL_PORT"]),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        database=DB_NAME, charset="utf8mb4", cursorclass=DictCursor, ssl=ssl,
        autocommit=autocommit, read_timeout=300, write_timeout=300)


def _columns(conn, table) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ORDINAL_POSITION",
            (DB_NAME, table))
        return [r["COLUMN_NAME"] for r in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--truncate", action="store_true",
                    help="empty the destination tables first (makes the copy re-runnable)")
    ap.add_argument("--tables", default="",
                    help="comma-separated subset; default is all of them")
    args = ap.parse_args(argv)

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or TABLES
    unknown = [t for t in tables if t not in TABLES]
    if unknown:
        print(f"unknown table(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    if os.environ["MYSQL_HOST"] in ("127.0.0.1", "localhost", "::1"):
        print("MYSQL_HOST is loopback -- this would copy the local database onto "
              "itself. Point .env at the managed database first.", file=sys.stderr)
        return 2

    src, dst = _source(), _target(autocommit=False)
    print(f"source : {LOCAL_USER}@{LOCAL_HOST}:{LOCAL_PORT}/{DB_NAME}")
    print(f"target : {os.environ['MYSQL_USER']}@{os.environ['MYSQL_HOST']}:"
          f"{os.environ['MYSQL_PORT']}/{DB_NAME}\n", flush=True)

    if args.truncate:
        with dst.cursor() as cur:
            for table in tables:
                cur.execute(f"TRUNCATE TABLE `{table}`")
        dst.commit()
        print(f"truncated {len(tables)} table(s) on the target\n", flush=True)

    grand = 0
    for table in tables:
        src_cols, dst_cols = _columns(src, table), _columns(dst, table)
        if src_cols != dst_cols:
            print(f"  !! {table}: column mismatch, skipped\n"
                  f"     local={src_cols}\n     target={dst_cols}", flush=True)
            continue

        # Columns named explicitly rather than positionally, so the copy stays
        # correct even if one side's column order drifts after a migration.
        collist = ", ".join(f"`{c}`" for c in src_cols)
        insert = (f"INSERT INTO `{table}` ({collist}) "
                  f"VALUES ({', '.join(['%s'] * len(src_cols))})")
        size = BATCH.get(table, DEFAULT_BATCH)

        started, moved, batch = time.time(), 0, []
        with src.cursor() as rcur, dst.cursor() as wcur:
            rcur.execute(f"SELECT {collist} FROM `{table}`")
            for row in rcur:
                batch.append(tuple(row[c] for c in src_cols))
                if len(batch) >= size:
                    wcur.executemany(insert, batch)
                    dst.commit()
                    moved += len(batch)
                    batch.clear()
            if batch:
                wcur.executemany(insert, batch)
                dst.commit()
                moved += len(batch)
        grand += moved
        print(f"  {table:<16}{moved:>8} rows  ({time.time() - started:.1f}s)", flush=True)

    print(f"\ncopied {grand} rows")
    src.close()
    dst.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
