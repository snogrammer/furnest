"""Scrubbing personal data out of a captured page before it becomes a test fixture.

Fixtures are committed, so a page recorded from a live source must not carry anyone's
email, phone number, name or exact address. :func:`sanitize` replaces them with the
placeholders the hand-written fixtures already use:

==========  ==========================  =============================================
Category    Placeholder                 Found by
==========  ==========================  =============================================
email       ``person@example.com``      regex, anywhere; ``example.*`` addresses kept
phone       ``555-555-0100``            regex for US numbers written with separators
                                        or after ``tel:``; JSON keys like ``phone``
name        ``Jane Example``            JSON keys (``firstName``, ``landlordName``,
                                        ``name`` inside a ``host``/``reviewer``/...
                                        object or a ``Person``), then every other
                                        occurrence of those values, plus ``redact``
address     ``123 Example St``          regex for ``<number> <Name> <St|Ave|...>``;
                                        JSON keys like ``streetAddress``
coordinate  rounded to 2 decimals       JSON keys ``lat``/``latitude``/``lng``/...
==========  ==========================  =============================================

How the body is read depends on its content type. JSON is parsed and walked, so a
key says what a value is. HTML gets the regex pass, and its embedded JSON
(``<script type="application/ld+json">``, Next.js ``__NEXT_DATA__``) is walked
like a JSON body first. Anything else, or JSON that doesn't parse, gets only the
regex pass.

Names in free text (a description signed "-- Bob") can't be found by pattern. Pass
them in ``redact``, and always read the diff before committing a fixture. Running
the sanitizer on its own output changes nothing.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

EMAIL_PLACEHOLDER = "person@example.com"
PHONE_PLACEHOLDER = "555-555-0100"
NAME_PLACEHOLDER = "Jane Example"
ADDRESS_PLACEHOLDER = "123 Example St"
COORDINATE_DECIMALS = 2

# A number under a phone key is only a phone number with at least 7 digits.
_MIN_PHONE_NUMBER = 1_000_000
# Names shorter than this are too likely to match ordinary words elsewhere.
_MIN_TERM_LENGTH = 3

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@((?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,}))\b")
# Domains reserved for documentation (RFC 2606), so they are already placeholders.
_SAFE_EMAIL_DOMAIN = re.compile(r"(?:^|\.)example\.(?:com|org|net)$|\.(?:example|invalid|test)$")
# ``logo@2x.png`` looks like an email but is an image name.
_FILE_SUFFIXES = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "avif"})
_PHONE = re.compile(
    r"(?<![\w-])(?:tel:\s*)?(?:\+?1[ .-]?)?(?:\(\d{3}\)[ ]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
    r"|tel:\s*\+?\d{10,11}\b",
    re.IGNORECASE,
)
_STREET_TYPES = (
    "Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Place|Pl"
    "|Terrace|Ter|Circle|Cir|Parkway|Pkwy|Highway|Hwy|Trail|Trl|Square|Sq"
)
_ADDRESS = re.compile(
    r"(?<![\w-])\d{1,6}(?:-\d{1,4})?[ \t]+(?:[NSEW]\.?[ \t]+)?"
    # Street name words: capitalized, or an ordinal such as 5th.
    r"(?:(?:[A-Z][\w'.]*|\d+(?:st|nd|rd|th))[ \t]+){1,4}"
    rf"(?:{_STREET_TYPES})\b\.?"
    r"(?:,?[ \t]+(?:Apt|Apartment|Unit|Suite|Ste|#)\.?[ \t]*#?[A-Za-z0-9-]+)?"
)
_SCRIPT_JSON = re.compile(
    r"(<script\b[^>]*\btype=[\"']application/(?:ld\+)?json[\"'][^>]*>)(.*?)(</script>)",
    re.IGNORECASE | re.DOTALL,
)

# JSON keys, compared lower-case with everything but letters and digits removed.
_EMAIL_KEY = "email"
_PHONE_KEYS = frozenset({"tel", "telephone", "mobile", "cell", "fax"})
_ADDRESS_KEYS = frozenset(
    {
        "address",
        "streetaddress",
        "street",
        "address1",
        "address2",
        "addressline1",
        "addressline2",
        "line1",
        "line2",
        "fulladdress",
        "formattedaddress",
    }
)
_PLACEHOLDERS = {
    "email": EMAIL_PLACEHOLDER,
    "phone": PHONE_PLACEHOLDER,
    "name": NAME_PLACEHOLDER,
    "address": ADDRESS_PLACEHOLDER,
}
_COORDINATE_KEYS = frozenset({"lat", "latitude", "lng", "lon", "long", "longitude"})
# A key ``<word>name`` holds a person's name for these words, and a plain ``name``
# does when its object sits under a key containing one of them.
_PERSON_WORDS = (
    "landlord",
    "host",
    "owner",
    "contact",
    "reviewer",
    "review",
    "author",
    "user",
    "agent",
    "manager",
    "tenant",
    "person",
    "guest",
    "member",
    "seller",
    "broker",
    "realtor",
)
_NAME_PREFIXES = (*_PERSON_WORDS, "first", "last", "full", "given", "family", "display", "middle")


@dataclass(frozen=True)
class Sanitized:
    text: str
    # Replacements made, by category (``email``, ``phone``, ``name``, ...).
    counts: Mapping[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def sanitize(body: str, content_type: str, *, redact: Iterable[str] = ()) -> Sanitized:
    """``body`` with personal data replaced; ``content_type`` picks how it's read."""
    return _Pass(redact).run(body, content_type.lower())


class _Pass:
    """One call's state: the names learned so far and the replacement counts."""

    def __init__(self, redact: Iterable[str]) -> None:
        self.counts: Counter[str] = Counter()
        self.terms: set[str] = set()
        self._names_pattern: re.Pattern[str] | None = None
        self._names_count = 0
        for term in redact:
            self._learn(term)

    def run(self, body: str, content_type: str) -> Sanitized:
        if "json" in content_type:
            text = self._json_body(body)
        elif "html" in content_type:
            text = self._html(body)
        else:
            text = self._text(body)
        return Sanitized(text, {k: v for k, v in sorted(self.counts.items()) if v})

    # --- Bodies -------------------------------------------------------------------

    def _json_body(self, body: str) -> str:
        try:
            data = json.loads(body)
        except ValueError:
            return self._text(body)
        self._collect(data, person=False)
        return json.dumps(self._value(data, person=False), indent=2, ensure_ascii=False) + "\n"

    def _html(self, body: str) -> str:
        blocks: dict[int, Any] = {}
        for match in _SCRIPT_JSON.finditer(body):
            try:
                blocks[match.start()] = json.loads(match.group(2))
            except ValueError:
                continue
            self._collect(blocks[match.start()], person=False)

        def rewrite(match: re.Match[str]) -> str:
            if match.start() not in blocks:
                return match.group(0)
            data = self._value(blocks[match.start()], person=False)
            # ``</`` inside a string would close the script element early.
            dumped = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
            return f"{match.group(1)}{dumped}{match.group(3)}"

        return self._text(_SCRIPT_JSON.sub(rewrite, body))

    # --- Free text ----------------------------------------------------------------

    def _text(self, text: str) -> str:
        text = _EMAIL.sub(self._email, text)
        text = _PHONE.sub(self._phone, text)
        text = _ADDRESS.sub(lambda m: self._replace(m, "address", ADDRESS_PLACEHOLDER), text)
        names = self._names()
        if names is not None:
            text = names.sub(lambda m: self._replace(m, "name", NAME_PLACEHOLDER), text)
        return text

    def _names(self) -> re.Pattern[str] | None:
        """One pattern for every known name, rebuilt only when a name is learned."""
        if not self.terms:
            return None
        if self._names_pattern is None or self._names_count != len(self.terms):
            # Longest first, so "Jane Smith" wins over a learned "Smith".
            alternatives = "|".join(re.escape(t) for t in sorted(self.terms, key=len, reverse=True))
            self._names_pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)
            self._names_count = len(self.terms)
        return self._names_pattern

    def _email(self, match: re.Match[str]) -> str:
        domain, suffix = match.group(1).lower(), match.group(2).lower()
        if _SAFE_EMAIL_DOMAIN.search(domain) or suffix in _FILE_SUFFIXES:
            return match.group(0)
        return self._replace(match, "email", EMAIL_PLACEHOLDER)

    def _phone(self, match: re.Match[str]) -> str:
        if match.group(0).lower().startswith("tel:"):
            return self._replace(match, "phone", f"tel:{PHONE_PLACEHOLDER}")
        return self._replace(match, "phone", PHONE_PLACEHOLDER)

    def _replace(self, match: re.Match[str], category: str, placeholder: str) -> str:
        if match.group(0) == placeholder:
            return placeholder
        self.counts[category] += 1
        return placeholder

    # --- JSON ---------------------------------------------------------------------

    def _collect(self, value: object, *, person: bool) -> None:
        """Learn the names held under name keys, to replace them in free text too."""
        if isinstance(value, dict):
            person = person or _is_person(value)
            for key, item in value.items():
                kind = _key_kind(str(key), person=person)
                if kind == "name" and isinstance(item, str):
                    self._learn(item)
                else:
                    self._collect(item, person=_is_person_key(str(key)))
        elif isinstance(value, list):
            for item in value:
                self._collect(item, person=person)

    def _value(self, value: object, *, person: bool) -> object:
        if isinstance(value, dict):
            person = person or _is_person(value)
            return {key: self._field(str(key), item, person=person) for key, item in value.items()}
        if isinstance(value, list):
            return [self._value(item, person=person) for item in value]
        if isinstance(value, str):
            return self._text(value)
        return value

    def _field(self, key: str, value: object, *, person: bool) -> object:
        kind = _key_kind(key, person=person)
        if kind == "coordinate":
            return self._coordinate(value)
        if kind is not None and _replaceable(kind, value):
            placeholder = _PLACEHOLDERS[kind]
            if value in ("", placeholder):
                return value
            self.counts[kind] += 1
            return placeholder
        return self._value(value, person=_is_person_key(key))

    def _coordinate(self, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return value
        try:
            number = float(value)
        except ValueError:
            return value
        rounded = round(number, COORDINATE_DECIMALS)
        if rounded == number:
            return value
        self.counts["coordinate"] += 1
        return str(rounded) if isinstance(value, str) else rounded

    def _learn(self, term: str) -> None:
        term = term.strip()
        if len(term) >= _MIN_TERM_LENGTH and term.lower() != NAME_PLACEHOLDER.lower():
            self.terms.add(term)


def _normalize(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_person_key(key: str) -> bool:
    normalized = _normalize(key)
    return any(word in normalized for word in _PERSON_WORDS)


def _is_person(value: dict[Any, Any]) -> bool:
    return value.get("@type") == "Person"


def _key_kind(key: str, *, person: bool) -> str | None:
    """What a value under ``key`` is, or ``None`` if the key doesn't say."""
    normalized = _normalize(key)
    if _EMAIL_KEY in normalized:
        return "email"
    if "phone" in normalized or normalized in _PHONE_KEYS:
        return "phone"
    if normalized in _ADDRESS_KEYS:
        return "address"
    if normalized in _COORDINATE_KEYS:
        return "coordinate"
    if normalized == "name":
        return "name" if person else None
    if normalized.endswith("name") and normalized.startswith(_NAME_PREFIXES):
        return "name"
    return None


def _replaceable(kind: str, value: object) -> bool:
    """Strings are replaced, and so are numbers under a phone key that are long enough
    to be one (``phoneCount: 2`` isn't). Flags like ``emailVerified: true`` and nested
    objects are left to the walk."""
    if isinstance(value, str):
        return True
    if kind != "phone" or isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(value) >= _MIN_PHONE_NUMBER
