"""Polite HTTP GET with retries + back-off, shared by every scraper.

Standard library only. Handles the three response shapes the scrapers need
(``bytes``, ``text``, parsed ``json``), backs off on 429/5xx (honouring
``Retry-After``), and optionally returns ``None`` for "gone" status codes
instead of raising.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_UA = "email-scrapper (polite; standard-library scraper)"


def fetch(
    url: str,
    *,
    parse: str = "bytes",
    tries: int = 4,
    ua: str = DEFAULT_UA,
    timeout: int = 45,
    accept: str | None = None,
    none_on: tuple[int, ...] = (404, 410),
):
    """GET ``url`` and return its body.

    parse:    "bytes" (raw), "text" (utf-8, replace errors), or "json".
    none_on:  HTTP status codes that yield ``None`` instead of raising
              (e.g. deleted/private pages). Pass ``()`` to always raise.

    Retries on 429/5xx with exponential-ish back-off (capped at 30s, honouring
    ``Retry-After``) and on transient network/JSON errors. Raises on the final
    attempt if the error is not recoverable.
    """
    headers = {"User-Agent": ua}
    if accept:
        headers["Accept"] = accept

    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if parse == "json":
                return json.loads(data)
            if parse == "text":
                return data.decode("utf-8", "replace")
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                wait = min(30, 2 * attempt)
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                time.sleep(wait)
                continue
            if e.code in none_on:
                return None
            if attempt == tries:
                raise
            time.sleep(1.5 * attempt)
        except (OSError, json.JSONDecodeError):
            # OSError is the superset of URLError, socket timeouts and the
            # connection resets servers hand out mid-run -- all retryable.
            if attempt == tries:
                raise
            time.sleep(1.5 * attempt)
    return None
