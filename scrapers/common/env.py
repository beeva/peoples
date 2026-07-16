"""Read the repo-root ``.env`` so scrapers pick up tokens when run standalone.

When a scraper is launched by ``server.py`` it inherits an environment the
server already populated from ``.env``. Run straight from a shell, though, it
would see nothing -- so it loads the same file itself. A real environment
variable always wins over the file.
"""
from __future__ import annotations

import os
from pathlib import Path

# scrapers/common/env.py -> scrapers/common -> scrapers -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from ``.env`` into ``os.environ`` (no overwrite)."""
    env_file = path or (REPO_ROOT / ".env")
    try:
        text = env_file.read_text(encoding="utf-8")
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
