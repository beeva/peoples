"""JSONL helpers for resumable scrapers (read, dedup keys, materialise array).

Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Iterator


def iter_jsonl(path: str, *, warn: bool = False) -> Iterator[dict]:
    """Yield each parsed JSON object from a JSONL file, skipping blanks.

    Missing file yields nothing. Malformed lines are skipped (optionally warned
    about on stderr).
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if warn:
                    print(f"[warn] skipping malformed line {line_no}", file=sys.stderr)


def load_done_keys(path: str, key: str) -> set:
    """Return the set of ``rec[key]`` values already present in a JSONL file.

    Used for resume support: re-running a scrape skips records already written.
    """
    return {rec[key] for rec in iter_jsonl(path) if key in rec}


def write_json_array(jsonl_path: str, json_path: str, cap: int = 100_000) -> None:
    """Materialise a JSONL file into a pretty JSON array (skipped if too large)."""
    if not os.path.exists(jsonl_path):
        return
    records = []
    for rec in iter_jsonl(jsonl_path):
        records.append(rec)
        if len(records) > cap:
            print(f"  > {cap} records -- skipping {json_path}; use {jsonl_path}",
                  file=sys.stderr)
            return
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  wrote {len(records)} records -> {json_path}", file=sys.stderr)
