"""Contact-email extraction from free text / HTML, shared by every scraper.

The three scrapers had independently-tuned copies of these regexes; this is
the union of them, so every scraper benefits from the full placeholder and
asset-file filtering.
"""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MAILTO_RE = re.compile(r"mailto:([^\s\"')?>\]]+)", re.I)

# Things that look like emails but are asset files (e.g. sprite@2x.png).
BAD_EMAIL_TLD = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|css|js|json|md|html?)$", re.I
)
# Common placeholders / non-contact addresses to drop.
PLACEHOLDER = re.compile(
    r"(example\.(com|org|net)|your[-_.]?email|email@|name@|user@|domain\.com"
    r"|sentry\.io|wixpress\.com|@2x|@3x)",
    re.I,
)


def extract_emails(text: str | None, *, include_mailto: bool = True) -> list[str]:
    """Return unique, plausible contact emails from text/HTML.

    By default ``mailto:`` links are folded in. Set ``include_mailto=False`` to
    scan only bare addresses (callers that track mailto separately).
    """
    candidates = list(EMAIL_RE.findall(text or ""))
    if include_mailto:
        candidates += MAILTO_RE.findall(text or "")
    found = set()
    for raw in candidates:
        email = raw.strip().strip(".,;:").lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        if BAD_EMAIL_TLD.search(email) or PLACEHOLDER.search(email):
            continue
        found.add(email)
    return sorted(found)


def extract_mailto(text: str | None) -> list[str]:
    """Return unique addresses from ``mailto:`` links only."""
    return sorted({m.lower() for m in MAILTO_RE.findall(text or "")})
