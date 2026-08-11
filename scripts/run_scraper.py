#!/usr/bin/env python3
"""Run one scraper by source name, with its arguments as a single string.

Used by .github/workflows/scrape.yml, which is how a scrape runs when the API
is deployed somewhere that cannot host an hours-long subprocess (see DEPLOY.md).

The argument string comes from server.py's `_remote_args`, which builds it with
the same `_scrape_argv` a local run uses and joins it with shlex.quote. Undoing
that with shlex.split here -- rather than letting the shell expand it -- is what
keeps a country name with a space in it in one piece, and means nothing in the
string can be read as a shell command.

    python scripts/run_scraper.py github "--target 1000 --countries 'United States'"
    python scripts/run_scraper.py phones "--limit 5000"
"""
import shlex
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# The one place a source name is turned into a script. The scrapers put their
# own directories on sys.path from __file__, so the working directory only has
# to be the project root.
SCRIPTS = {
    "github": "scrapers/github/github_scrape.py",
    "aboutme": "scrapers/aboutme/aboutme_scrape.py",
    "devto": "scrapers/devto/devto_scrape.py",
    "discourse": "scrapers/discourse/discourse_scrape.py",
    "phones": "scrapers/phone_pass.py",
}


def main() -> int:
    source = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if source not in SCRIPTS:
        print(f"unknown source {source!r}; expected one of "
              f"{', '.join(sorted(SCRIPTS))}", file=sys.stderr)
        return 2
    raw = " ".join(sys.argv[2:])
    script = BASE_DIR / SCRIPTS[source]
    if not script.exists():
        print(f"missing scraper: {script}", file=sys.stderr)
        return 2
    argv = [sys.executable, "-u", str(script), *shlex.split(raw)]
    print("+ " + " ".join(shlex.quote(a) for a in argv), flush=True)
    # The child's return code is this script's: a failed scrape has to fail the
    # workflow step, or a broken run would be reported as a successful one.
    return subprocess.run(argv, cwd=str(BASE_DIR)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
