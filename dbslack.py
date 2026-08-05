#!/usr/bin/env python3
"""Slack workspace exports, stored in and served from MySQL.

These are not scraped by this app -- they are export files dropped into
``scrapers/slack/users/`` by hand -- but they are contact data, so they belong
in the same place as everything else rather than being read off disk by the web
app on every page load.

Each user is kept whole in ``slack_users.data``; the web app does its own
flattening and cross-workspace grouping, and this deliberately does not
second-guess it. Only the two fields the app needs to index by -- the Slack user
id and the email -- are lifted into columns.

    python dbslack.py import [dir]   # load the export files
    python dbslack.py status         # what is stored
"""
import json
import sys
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DIR = BASE_DIR / "scrapers" / "slack" / "users"


def _user_key(user: dict, index: int) -> str:
    """Stable per-workspace key: the Slack id, or the position as a fallback."""
    uid = user.get("id")
    return str(uid) if uid else f"idx:{index}"


def import_dir(source_dir: Path | str = DEFAULT_DIR, *,
               verbose: bool = True) -> dict:
    """Load every *.json workspace export into the database.

    Idempotent: a workspace is replaced wholesale by its file, so re-importing
    an updated export is just running this again.
    """
    source_dir = Path(source_dir)
    try:
        files = sorted(p for p in source_dir.iterdir() if p.suffix == ".json")
    except OSError:
        files = []
    if not files:
        if verbose:
            print(f"[slack] no export files in {source_dir}")
        return {"ok": True, "workspaces": [], "users": 0}

    summary = []
    total = 0
    for path in files:
        try:
            users = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[slack] {path.name}: {e}", file=sys.stderr)
            continue
        if not isinstance(users, list):
            print(f"[slack] {path.name}: not a JSON array -- skipped",
                  file=sys.stderr)
            continue

        slug = path.stem
        ws = (users[0].get("workspace") if users else None) or {}
        name = str(ws.get("name") or slug)

        rows = []
        for i, u in enumerate(users):
            if not isinstance(u, dict):
                continue
            profile = u.get("profile") or {}
            email = str(profile.get("email") or "").strip().lower()
            rows.append((slug[:190], name[:255], _user_key(u, i)[:190],
                         email[:190], i,
                         json.dumps(u, ensure_ascii=False)))

        with db.transaction():
            db.execute("DELETE FROM slack_users WHERE workspace_slug = %s", (slug,))
            db.insert_chunked(
                "INSERT INTO slack_users (workspace_slug, workspace_name, "
                "  user_key, email, pos, data) VALUES (%s, %s, %s, %s, %s, %s)",
                rows)
        summary.append({"slug": slug, "name": name, "count": len(rows)})
        total += len(rows)
        if verbose:
            print(f"[slack] {name}: {len(rows)} users")

    return {"ok": True, "workspaces": summary, "users": total}


def workspaces() -> list[dict]:
    """Every stored workspace with its display name and user count."""
    rows = db.query(
        "SELECT workspace_slug AS slug, MAX(workspace_name) AS name, "
        "       COUNT(*) AS n FROM slack_users GROUP BY workspace_slug")
    out = [{"slug": r["slug"], "name": r["name"] or r["slug"],
            "count": int(r["n"])} for r in rows]
    out.sort(key=lambda w: w["name"].lower())
    return out


def users(slug: str) -> list[dict]:
    """One workspace's users, in the order the export listed them."""
    return [json.loads(r["data"] or "{}") for r in db.query(
        "SELECT data FROM slack_users WHERE workspace_slug = %s ORDER BY pos",
        (slug,))]


def count_all() -> int:
    """Distinct people across every workspace, deduplicated by email.

    Someone with no email on their profile cannot be matched to a person in
    another workspace, so they count once per workspace they appear in -- which
    is what the file-based version did.
    """
    with_email = int(db.scalar(
        "SELECT COUNT(DISTINCT email) AS n FROM slack_users WHERE email <> ''",
        default=0))
    without = int(db.scalar(
        "SELECT COUNT(*) AS n FROM slack_users WHERE email = ''", default=0))
    return with_email + without


def status() -> dict:
    return {"workspaces": workspaces(), "unique_people": count_all()}


def main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    db.bootstrap(verbose=False)
    if cmd == "import":
        result = import_dir(argv[1] if len(argv) > 1 else DEFAULT_DIR)
        print(f"[slack] {result['users']} users across "
              f"{len(result['workspaces'])} workspace(s)")
        return 0
    if cmd == "status":
        st = status()
        for w in st["workspaces"]:
            print(f"  {w['name']:<28} {w['count']}")
        print(f"  {'unique people':<28} {st['unique_people']}")
        return 0
    print(f"unknown command '{cmd}' (expected import or status)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
