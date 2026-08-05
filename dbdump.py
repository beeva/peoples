#!/usr/bin/env python3
"""Export the database to a .sql file, and restore one back into it.

Backups, moving the archive to another machine, and rolling back a bad scrape
all want the same thing: one portable file holding schema and data. XAMPP
already ships `mysqldump` and `mysql`, so this drives those rather than
inventing a format -- the output is an ordinary dump that phpMyAdmin, another
MySQL server, or a plain `mysql <` will all accept.

    python dbdump.py export [file.sql]     # write a dump
    python dbdump.py import <file.sql>     # restore one
    python dbdump.py status                # what is in the database now

The same three operations are exposed over HTTP by server.py (/api/db/export,
/api/db/import, /api/db/status) and wired to `npm run db:export` / `db:import`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent
# Dumps land here by default. Git-ignored: they are large and reproducible.
BACKUP_DIR = Path(os.environ.get("MYSQL_BACKUP_DIR", BASE_DIR / "backups"))

# Where XAMPP keeps mysqldump/mysql. Same search order as scripts/mysql-server.js.
CANDIDATE_BASEDIRS = [
    os.environ.get("MYSQL_BASEDIR", ""),
    "D:/xampp/mysql", "C:/xampp/mysql", "E:/xampp/mysql",
]


def _tool(name: str) -> str:
    """Absolute path to a MySQL client binary, or bare name if it is on PATH."""
    exe = f"{name}.exe" if os.name == "nt" else name
    for base in CANDIDATE_BASEDIRS:
        if not base:
            continue
        candidate = Path(base) / "bin" / exe
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"could not find {exe}. Set MYSQL_BASEDIR in .env to your XAMPP mysql "
        f"folder (the one containing bin/{exe})."
    )


def _env() -> dict:
    """Child environment carrying the password out of sight of `ps`/Task Manager."""
    env = dict(os.environ)
    if db.DB_PASSWORD:
        env["MYSQL_PWD"] = db.DB_PASSWORD
    return env


def _run(argv: list[str], *, stdout=None, stdin=None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        argv, stdout=stdout, stdin=stdin, stderr=subprocess.PIPE,
        env=_env(), cwd=str(BASE_DIR),
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        # mysqldump warns about this on every single run; it is not an error.
        err = "\n".join(l for l in err.splitlines()
                        if "Using a password on the command line" not in l)
        raise RuntimeError(err or f"{Path(argv[0]).name} exited with "
                                  f"code {proc.returncode}")
    return proc


def _auth_flags() -> list[str]:
    """Connection flags. The password travels in MYSQL_PWD, not argv."""
    return [f"--host={db.DB_HOST}", f"--port={db.DB_PORT}", f"--user={db.DB_USER}"]


def default_filename() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{db.DB_NAME}-{stamp}.sql"


def export_sql(target: str | None = None) -> dict:
    """Dump the whole database to a .sql file. Returns where it landed."""
    path = Path(target) if target else BACKUP_DIR / default_filename()
    if path.is_dir():
        path = path / default_filename()
    path.parent.mkdir(parents=True, exist_ok=True)

    argv = [_tool("mysqldump"), *_auth_flags(),
            "--databases", db.DB_NAME,
            # Recreate the database on restore, so a dump is a complete
            # description of the archive rather than a diff against one.
            "--add-drop-database", "--add-drop-table",
            "--default-character-set=utf8mb4",
            # One INSERT per chunk, kept small enough for a stock XAMPP server
            # whose max_allowed_packet is still 1MB.
            "--extended-insert", "--net-buffer-length=32768",
            "--max-allowed-packet=64M",
            # A consistent snapshot without locking out a running scrape.
            "--single-transaction", "--quick",
            "--skip-comments" if os.environ.get("MYSQL_DUMP_TERSE") else "--comments",
            ]

    # Written to a temp file first, so a failure part-way cannot leave a
    # truncated dump sitting where a restore might pick it up.
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with tmp.open("wb") as out:
            _run(argv, stdout=out)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    size = path.stat().st_size
    return {"path": str(path), "filename": path.name, "bytes": size,
            "database": db.DB_NAME,
            "created_at": datetime.now().isoformat(timespec="seconds")}


def import_sql(path: str = "", sql_bytes: bytes | None = None) -> dict:
    """Restore a .sql file (a path on this machine, or raw uploaded bytes).

    The dump is fed to the `mysql` client rather than parsed here: splitting SQL
    correctly means handling delimiters, strings and comments, and the client
    that MySQL ships already does it properly.
    """
    temp_path = None
    if sql_bytes is not None:
        if not sql_bytes.strip():
            raise RuntimeError("no SQL was uploaded")
        fd, temp_path = tempfile.mkstemp(suffix=".sql", prefix="restore-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(sql_bytes)
        source = Path(temp_path)
    else:
        source = Path(path)
        if not source.is_file():
            raise RuntimeError(f"no such file: {source}")

    size = source.stat().st_size
    # Restoring into a server that has never seen this database (a fresh
    # machine, or straight after a DROP) has to have something to connect to
    # before the dump's own CREATE DATABASE runs.
    db.ensure_database()
    # A dump made with --databases carries its own CREATE DATABASE/USE, but one
    # made from a single table may not, so connect to the database by default.
    argv = [_tool("mysql"), *_auth_flags(),
            "--default-character-set=utf8mb4",
            f"--max-allowed-packet={os.environ.get('MYSQL_MAX_PACKET', '64M')}",
            db.DB_NAME]
    try:
        with source.open("rb") as fh:
            _run(argv, stdin=fh)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    # A restore can bring in a schema older than this build expects, so the DDL
    # is re-run against it. If the dump predates the current schema version the
    # ingested tables are dropped and rebuilt -- which leaves them empty until
    # the scraper files are read back in, so that is done here rather than
    # handing back a database with no contacts in it.
    import dbsync

    db._BOOTSTRAPPED = False  # noqa: SLF001 -- re-run DDL against restored data
    db.bootstrap(verbose=False)
    resynced = []
    if int(db.scalar("SELECT COUNT(*) AS n FROM contacts", default=0)) == 0:
        resynced = dbsync.sync_all(verbose=False).get("changed", [])
    return {"restored": str(source if not temp_path else "(uploaded)"),
            "bytes": size, "database": db.DB_NAME, "resynced": resynced}


def list_backups() -> list[dict]:
    """Dumps already sitting in the backup folder, newest first."""
    try:
        files = [p for p in BACKUP_DIR.iterdir()
                 if p.is_file() and p.suffix == ".sql"]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{
        "filename": p.name,
        "path": str(p),
        "bytes": p.stat().st_size,
        "created_at": datetime.fromtimestamp(p.stat().st_mtime)
                              .isoformat(timespec="seconds"),
    } for p in files[:50]]


def status() -> dict:
    """Everything the Database page shows about the server and its contents."""
    import dbsync  # local import: dbsync imports db, and db must load first
    try:
        version = db.server_version()
        reachable = True
    except Exception:  # noqa: BLE001
        version, reachable = "", False
    return {
        "connected": reachable,
        "host": db.DB_HOST,
        "port": db.DB_PORT,
        "user": db.DB_USER,
        "database": db.DB_NAME,
        "server": version,
        "size_bytes": db.database_size_bytes() if reachable else 0,
        "tables": db.table_counts() if reachable else {},
        "sources": dbsync.sync_status() if reachable else [],
        "backup_dir": str(BACKUP_DIR),
        "backups": list_backups(),
    }


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    rest = argv[1:]
    db.bootstrap(verbose=False)

    if cmd == "export":
        result = export_sql(rest[0] if rest else None)
        print(f"[db] exported {result['database']} -> {result['path']} "
              f"({_human(result['bytes'])})")
        return 0

    if cmd == "import":
        if not rest:
            print("usage: python dbdump.py import <file.sql>", file=sys.stderr)
            return 2
        result = import_sql(path=rest[0])
        print(f"[db] restored {result['restored']} into {result['database']} "
              f"({_human(result['bytes'])})")
        counts = db.table_counts()
        print(f"[db] now holds {counts['contacts']} contacts "
              f"from {counts['records']} records")
        return 0

    if cmd == "status":
        st = status()
        state = "connected" if st["connected"] else "UNREACHABLE"
        print(f"[db] {st['user']}@{st['host']}:{st['port']}/{st['database']} "
              f"({state}, MySQL {st['server']}, {_human(st['size_bytes'])})")
        for name, n in st["tables"].items():
            print(f"       {name:<16} {n}")
        for s in st["sources"]:
            flag = "STALE" if s["stale"] else "in sync"
            print(f"       {s['source']:<16} {s['contacts']} contacts "
                  f"from {s['records']} records ({flag})")
        if st["backups"]:
            print(f"[db] {len(st['backups'])} dump(s) in {st['backup_dir']}")
        return 0

    print(f"unknown command '{cmd}' (expected export, import or status)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
