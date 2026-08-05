"""Shared building blocks for the scrapers.

All three scrapers (devto, discourse, aboutme) hit JSON/XML endpoints, pull
contact emails out of free text, and write resumable JSONL. Those pieces live
here so each scraper is just its site-specific glue.

Scrapers run as standalone scripts, so they add the ``scrapers/`` directory to
``sys.path`` and then ``from common import ...``.
"""
from .http import DEFAULT_UA, fetch
from .emails import (
    EMAIL_PROVIDERS,
    email_provider,
    email_rank,
    extract_emails,
    extract_mailto,
    is_personal_email,
    is_role_email,
    personal_first,
)
from .env import load_env
from .jsonl import iter_jsonl, load_done_keys, write_json_array

__all__ = [
    "DEFAULT_UA",
    "fetch",
    "load_env",
    "EMAIL_PROVIDERS",
    "email_provider",
    "email_rank",
    "extract_emails",
    "extract_mailto",
    "is_personal_email",
    "is_role_email",
    "personal_first",
    "iter_jsonl",
    "load_done_keys",
    "write_json_array",
]
