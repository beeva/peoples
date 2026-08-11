"""Phone- and WhatsApp-number extraction from free text / HTML.

The counterpart to ``emails.py``, and deliberately much stricter than it. An
email address carries its own proof that it is one -- the ``@`` and a TLD. A
phone number is just digits, and free text is full of digits that are not
phone numbers: dates, version strings, order ids, follower counts, dimensions.
Extracting loosely would fill the archive with contacts nobody can call.

So a run of digits is only accepted when something vouches for it:

  * a ``wa.me`` / ``api.whatsapp.com`` link, or a ``tel:`` link -- the markup
    says what it is;
  * the word "whatsapp" (or "phone", "call", "mobile", ...) immediately before
    it, in any of several languages;
  * a leading ``+`` followed by a real country calling code.

Anything else -- a bare local number with no country context, "2024-01-15",
"v1.2.3.4" -- is dropped. That trades recall for precision on purpose.

Numbers are normalised to E.164 (``+`` and digits, no spaces) so the same
person written three different ways de-duplicates to one contact.
"""
from __future__ import annotations

import re

# ---- country calling codes ------------------------------------------------
# Every assigned ITU-T E.164 country code. Used to validate that a leading
# "+..." really is a country and not, say, a temperature range or a diff stat.
# Longest-first matching matters: +1 (US) must not shadow +1242 (Bahamas), and
# +7 (Russia) must not shadow +7[0-9] shared ranges.
CALLING_CODES = {
    # 1-digit
    "1", "7",
    # 2-digit
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43",
    "44", "45", "46", "47", "48", "49", "51", "52", "53", "54", "55", "56",
    "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84",
    "86", "90", "91", "92", "93", "94", "95", "98",
    # 3-digit
    "211", "212", "213", "216", "218", "220", "221", "222", "223", "224",
    "225", "226", "227", "228", "229", "230", "231", "232", "233", "234",
    "235", "236", "237", "238", "239", "240", "241", "242", "243", "244",
    "245", "246", "248", "249", "250", "251", "252", "253", "254", "255",
    "256", "257", "258", "260", "261", "262", "263", "264", "265", "266",
    "267", "268", "269", "290", "291", "297", "298", "299", "350", "351",
    "352", "353", "354", "355", "356", "357", "358", "359", "370", "371",
    "372", "373", "374", "375", "376", "377", "378", "379", "380", "381",
    "382", "383", "385", "386", "387", "389", "420", "421", "423", "500",
    "501", "502", "503", "504", "505", "506", "507", "508", "509", "590",
    "591", "592", "593", "594", "595", "596", "597", "598", "599", "670",
    "672", "673", "674", "675", "676", "677", "678", "679", "680", "681",
    "682", "683", "685", "686", "687", "688", "689", "690", "691", "692",
    "850", "852", "853", "855", "856", "870", "880", "886", "960", "961",
    "962", "963", "964", "965", "966", "967", "968", "970", "971", "972",
    "973", "974", "975", "976", "977", "992", "993", "994", "995", "996",
    "998",
    # North American Numbering Plan members reachable as +1NPA -- listed so a
    # +1 number is accepted on its own terms; the NPA check below does the rest.
}
_CODES_BY_LEN = sorted(CALLING_CODES, key=len, reverse=True)

# E.164 allows 15 digits including the country code; nothing real is under 8.
MIN_DIGITS = 8
MAX_DIGITS = 15
# Digits left after the country code. The shortest national numbers in use
# (Niue, Tokelau) are 4, but at that length far too much noise qualifies, so
# this is set where real contact numbers actually live.
MIN_NATIONAL_DIGITS = 6

# Countries whose national numbers legitimately keep a leading 0 in E.164.
# Italy is the well-known one (+39 06 ... is Rome); everywhere else a 0 right
# after the country code means the domestic trunk prefix was left in by mistake.
TRUNK_ZERO_COUNTRIES = {"39"}

# ---- link forms -----------------------------------------------------------
# wa.me/<number>, api.whatsapp.com/send?phone=<number>, whatsapp://send?phone=
WHATSAPP_LINK_RE = re.compile(
    r"(?:https?://)?(?:"
    r"(?:api\.|web\.|chat\.)?whatsapp\.com/(?:send/?\?[^\s\"'<>]*?\bphone=|send/?\?)?"
    r"|wa\.me/"
    r"|whatsapp://send\?[^\s\"'<>]*?\bphone="
    r")\+?(\d[\d\s().-]{5,20})",
    re.I,
)
TEL_LINK_RE = re.compile(r"tel:\s*(\+?[\d\s().-]{7,25})", re.I)
CALLTO_RE = re.compile(r"callto:\s*(\+?[\d\s().-]{7,25})", re.I)

# ---- labelled numbers -----------------------------------------------------
# A number is trusted when a word right before it says what it is. Covers the
# languages the archive actually contains (the scrapers cover Europe, the
# Americas and parts of Asia).
LABEL_WORDS = (
    r"whats\s?app|whatsapp|wa|telegram|viber|signal|imo"
    r"|phone|tel|telephone|mobile|cell|cellphone|call|contact|hotline|fax"
    r"|tlf|telefon|telefono|teléfono|telefone|téléphone|telefoon|puhelin"
    r"|handy|movil|móvil|celular|numero|número|numer|nr|tlf\.?|kontakt"
    r"|电话|手机|연락처|전화|携帯|電話"
)
LABELLED_RE = re.compile(
    rf"(?:{LABEL_WORDS})\b[\s:：.\-–—>|]{{0,12}}(\+?\d[\d\s().\-]{{6,22}})",
    re.I,
)
# Which labels mean the number is reachable on WhatsApp specifically. Matched
# against the label text *and its separator* ("WA: ", "WhatsApp - "), so the
# bare "wa" form needs word boundaries rather than string anchors.
WHATSAPP_LABEL_RE = re.compile(r"whats\s?app|\bwa\b", re.I)

# A "+" followed by digits is self-describing: nobody writes a date that way.
# The lookbehind is what keeps that true. A "+" glued to the end of a word is
# not a dialling plus, and the web is full of them -- a stylesheet's
# `unicode-range: U+2016-2017` reads as "+20162017", a valid-looking Egyptian
# number, and the same font is served to thousands of sites, so one missing
# lookbehind puts one wrong number on thousands of contacts. Nor is "++1234"
# a country code.
INTL_RE = re.compile(r"(?<![\w+])\+\s?(\d[\d\s().-]{6,22}\d)")

# ---- rejects --------------------------------------------------------------
# Repdigits and straight runs: 0000000000, 1234567890, +1 111 111 1111. Real
# numbers do exist in these shapes, but in scraped text they are placeholders.
_REPEATED = re.compile(r"^(\d)\1+$")
_SEQUENTIAL = "01234567890123456789"
# Numbers people write as examples.
PLACEHOLDER_NUMBERS = {
    "10000000000", "12345678900", "11111111111", "1234567890",
    "18005551212", "15551234567", "441234567890", "919876543210",
    "12025550100", "15550000000",
}
# US/NANP 555-01xx is reserved for fiction; anything in it is not a person.
_NANP_FICTIONAL = re.compile(r"^1\d{3}555(?:01\d{2})$")
# Two four-digit years joined by a dash: "2016-2017", "1998 - 2004". Eight
# digits is a plausible length for several countries, so a copyright line or a
# CV date range that reaches this far would otherwise be kept as a number.
_YEAR_RANGE = re.compile(
    r"^\+?\s*((?:19|20)\d{2})\s*[-–—/]\s*((?:19|20)\d{2})\s*$")
# The same range once the separator is gone -- which is how it survives in an
# archive stored before the rule above existed, and in a `unicode-range` where
# the dash was never there to begin with. Both halves have to read as years and
# the second cannot precede the first, because a range runs forwards. That does
# cost a real eight-digit number written as two ascending 19xx/20xx groups; no
# country whose subscriber numbers are that short uses a +19/+20 prefix, so the
# trade is a hypothetical loss against a demonstrated one.
_YEAR_RANGE_DIGITS = re.compile(r"^((?:19|20)\d{2})((?:19|20)\d{2})$")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _country_code(digits: str) -> str:
    """The country calling code this number starts with, or "" if none does."""
    for code in _CODES_BY_LEN:
        if digits.startswith(code):
            return code
    return ""


def normalise(raw: str, *, assume_intl: bool = False) -> str:
    """A candidate as E.164 (``+<digits>``), or "" if it is not a real number.

    ``assume_intl`` is for candidates that arrived without a ``+`` but from a
    context that guarantees an international number anyway -- the digits in a
    ``wa.me`` link are always full international, by WhatsApp's own rules.
    """
    text = (raw or "").strip()
    had_plus = text.startswith("+") or assume_intl
    digits = _digits(text)
    if not digits:
        return ""
    # Judged on the text first, since the separator is the clearest evidence,
    # then on the digits for candidates that have already lost it.
    if _YEAR_RANGE.match(text):
        return ""
    m = _YEAR_RANGE_DIGITS.match(digits)
    if m and m.group(2) >= m.group(1):
        return ""
    # A leading 00 is the other way of writing "+".
    if not had_plus and digits.startswith("00"):
        digits, had_plus = digits[2:], True
    if not had_plus:
        # No country context: cannot be placed on the map, so not kept. This is
        # the single biggest source of false positives, and dropping it is what
        # keeps the rest trustworthy.
        return ""
    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        return ""
    code = _country_code(digits)
    if not code:
        return ""
    # The country code on its own proves nothing -- "+1 555 0100" has a valid
    # code and still is not dialable. What is left after it has to be long
    # enough to be a real subscriber number.
    national = digits[len(code):]
    # A national number does not keep the trunk prefix in E.164: "+44 01234..."
    # is someone pasting the domestic form after a country code, and the result
    # dials nowhere. Italy is the standing exception -- its landlines really do
    # keep the leading 0 (+39 06 ... is Rome).
    if national.startswith("0") and code not in TRUNK_ZERO_COUNTRIES:
        return ""
    if code == "1":
        # North American Numbering Plan: exactly 10 digits, and neither the
        # area code nor the exchange may start with 0 or 1.
        if len(national) != 10:
            return ""
        if national[0] in "01" or national[3] in "01":
            return ""
    elif len(national) < MIN_NATIONAL_DIGITS:
        return ""
    if _REPEATED.match(digits) or digits in PLACEHOLDER_NUMBERS:
        return ""
    if _NANP_FICTIONAL.match(digits):
        return ""
    if digits in _SEQUENTIAL or digits[::-1] in _SEQUENTIAL:
        return ""
    return "+" + digits


def _add(found: dict, number: str, whatsapp: bool, origin: str) -> None:
    """Record a number, keeping the strongest claim made about it.

    The same number often appears twice -- once as a bare string and once in a
    wa.me link. Whichever occurrence proves WhatsApp wins, and the first origin
    seen is kept because the scan runs most-trustworthy-first.
    """
    if not number:
        return
    entry = found.get(number)
    if entry is None:
        found[number] = {"number": number, "whatsapp": whatsapp, "via": origin}
    elif whatsapp and not entry["whatsapp"]:
        entry["whatsapp"] = True
        entry["via"] = origin


def extract_phones(text: str | None) -> list[dict]:
    """Every phone number in some text, as ``{number, whatsapp, via}``.

    ``number`` is E.164. ``whatsapp`` says the number was published *as* a
    WhatsApp contact, not merely that it might work there. ``via`` records what
    vouched for it, which is worth keeping: a wa.me link is a much warmer lead
    than a number found loose in a bio.

    Ordered strongest evidence first, so ``[0]`` is the best number to use.
    """
    if not text:
        return []
    found: dict = {}

    # 1. Links. The markup states what the number is, so these are certain.
    for m in WHATSAPP_LINK_RE.finditer(text):
        _add(found, normalise(m.group(1), assume_intl=True), True, "wa-link")
    for rx in (TEL_LINK_RE, CALLTO_RE):
        for m in rx.finditer(text):
            _add(found, normalise(m.group(1)), False, "tel-link")

    # 2. Labelled numbers -- "WhatsApp: +62 812 ...", "Tel. +44 ...".
    for m in LABELLED_RE.finditer(text):
        label = text[max(0, m.start()):m.start(1)]
        is_wa = bool(WHATSAPP_LABEL_RE.search(label))
        # A label is good enough to treat a bare number as international only
        # when it already carries a "+"; otherwise it is a local number and we
        # have no country to attach to it.
        _add(found, normalise(m.group(1)), is_wa, "label")

    # 3. Bare international numbers.
    for m in INTL_RE.finditer(text):
        _add(found, normalise("+" + m.group(1)), False, "intl")

    order = {"wa-link": 0, "tel-link": 1, "label": 2, "intl": 3}
    return sorted(found.values(), key=lambda p: (order.get(p["via"], 9),
                                                 not p["whatsapp"], p["number"]))


def extract_whatsapp(text: str | None) -> list[str]:
    """Just the numbers published as WhatsApp contacts."""
    return [p["number"] for p in extract_phones(text) if p["whatsapp"]]


def as_entry(value) -> dict | None:
    """Coerce whatever a record carries into an entry, or None if it is not one.

    Phone lists come back out of ``records.raw`` -- JSON written by whichever
    version of whichever scraper was running at the time -- so they cannot be
    assumed to be well-formed dicts. A bare string is read as the number it
    looks like; anything that does not normalise is dropped rather than allowed
    to reach ``p["number"]`` and take a whole ingest down with it.
    """
    if isinstance(value, str):
        number = normalise(value)
        return {"number": number, "whatsapp": False, "via": "intl"} if number else None
    if not isinstance(value, dict):
        return None
    number = normalise(str(value.get("number") or ""))
    if not number:
        return None
    return {"number": number, "whatsapp": bool(value.get("whatsapp")),
            "via": str(value.get("via") or "intl")}


def phone_first(phones) -> list[dict]:
    """Order a person's numbers so the one to message leads.

    A WhatsApp number outranks a plain one: it is a channel the person chose to
    publish for being contacted on, and it is reachable without a phone call.
    """
    entries = [e for e in (as_entry(p) for p in phones or []) if e]
    order = {"wa-link": 0, "tel-link": 1, "label": 2, "intl": 3}
    return sorted(entries,
                  key=lambda p: (not p["whatsapp"], order.get(p["via"], 9),
                                 p["number"]))


def merge_phones(*groups) -> list[dict]:
    """Union several phone lists, keeping the strongest claim per number."""
    found: dict = {}
    for group in groups:
        for p in group or []:
            entry = as_entry(p)
            if entry:
                _add(found, entry["number"], entry["whatsapp"], entry["via"])
    return phone_first(list(found.values()))
