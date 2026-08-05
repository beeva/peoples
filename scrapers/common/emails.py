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


# ---- which of a person's addresses to write to ----------------------------
# Role addresses: deliverable, but they reach a desk rather than a person. For
# outreach that is the wrong end of the building, so they always sort last.
ROLE_RE = re.compile(
    r"^(support|help|info|contact|hello|hi|team|admin|sales|marketing|press"
    r"|careers|jobs|billing|legal|privacy|security|abuse|webmaster|postmaster"
    r"|office|mail|enquiries|inquiries|feedback|donate|hey|noreply|no-reply)@",
    re.I,
)
# Mailboxes the person owns themselves, rather than one their employer owns.
# These outrank a work address: the employer's may stop being theirs at any
# time, and it is read at a desk they may not want cold mail at. A `gmail.com`
# is a person; a `bigcorp.com` might be a shared alias with a personal-looking
# name. Wildcarded suffixes (hotmail.co.uk, yandex.ru, ...) via `endswith`.
PERSONAL_MAIL_DOMAINS = (
    "gmail.com", "googlemail.com", "outlook.", "hotmail.", "live.",
    "msn.com", "yahoo.", "ymail.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "pm.me", "tutanota.com",
    "tuta.io", "gmx.", "web.de", "mail.com", "mail.ru", "yandex.",
    "zoho.com", "fastmail.com", "hey.com", "engineer.com", "qq.com",
    "163.com", "126.com", "naver.com", "daum.net", "seznam.cz", "wp.pl",
    "o2.pl", "interia.pl", "libero.it", "orange.fr", "free.fr",
    "laposte.net", "bol.com.br", "uol.com.br", "terra.com.br",
)


# The consumer families worth telling apart when picking who to write to --
# a Gmail inbox behaves nothing like a Hotmail one for deliverability. Order
# matters only in that the buckets don't overlap; everything else personal
# falls through to "personal", and anything not personal at all to "company".
PROVIDER_DOMAINS = (
    ("gmail", ("gmail.com", "googlemail.com")),
    ("outlook", ("outlook.", "live.", "msn.com")),
    ("hotmail", ("hotmail.",)),
)

# Bucket keys in the order the UI lists them, with the label each carries.
EMAIL_PROVIDERS = (
    ("gmail", "Gmail"),
    ("outlook", "Outlook"),
    ("hotmail", "Hotmail"),
    ("personal", "Other personal"),
    ("company", "Company"),
)


def _domain_of(email: str) -> str:
    """The part after the last "@", or "" if there isn't one."""
    _, at, domain = (email or "").strip().lower().rpartition("@")
    return domain if at else ""


def _domain_matches(domain: str, known) -> bool:
    """Match a domain against a family list.

    An entry ending in a dot is a family: "yandex." matches yandex.ru and
    yandex.com, "hotmail." every country's hotmail. Anything else is exact.
    """
    for entry in known:
        if entry.endswith("."):
            if domain.startswith(entry):
                return True
        elif domain == entry:
            return True
    return False


def is_role_email(email: str) -> bool:
    """True for support@/info@/... -- a desk, not a person."""
    return bool(ROLE_RE.match((email or "").strip()))


def is_personal_email(email: str) -> bool:
    """True for a mailbox the person owns (gmail, proton, gmx, ...)."""
    domain = _domain_of(email)
    return bool(domain) and _domain_matches(domain, PERSONAL_MAIL_DOMAINS)


def email_provider(email: str) -> str:
    """Which mailbox bucket an address falls in -- see ``EMAIL_PROVIDERS``.

    "company" is the catch-all for a domain no consumer provider owns, which
    in practice is an employer's or the person's own project domain. Returns
    "" for something with no domain at all.
    """
    domain = _domain_of(email)
    if not domain:
        return ""
    for key, known in PROVIDER_DOMAINS:
        if _domain_matches(domain, known):
            return key
    return "personal" if _domain_matches(domain, PERSONAL_MAIL_DOMAINS) else "company"


def email_rank(email: str) -> tuple[int, int]:
    """Sort key: personal mailbox first, then work address, role address last.

    Only the *relative* order matters -- callers sort with this and keep their
    own tiebreak (for the scrapers, which source an address came from). Being a
    stable sort, an address that ties keeps the order the caller gave it.
    """
    return (1 if is_role_email(email) else 0,
            0 if is_personal_email(email) else 1)


def personal_first(emails) -> list[str]:
    """Re-order a person's addresses so the one to write to leads.

    Stable, so whatever order the caller established (for GitHub, how much the
    source is trusted: a profile email over a commit email) survives as the
    tiebreak within a rank.
    """
    return sorted([e for e in (emails or []) if e], key=email_rank)
