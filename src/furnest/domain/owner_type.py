"""Owner types: who is behind a listing, and how to tell from what a source gives us.

A renter cares whether they are dealing with a private landlord, a management
company or a broker (#113). No source states this outright, so adapters pass a *hint*
from structured signals (Zillow's multifamily flags, an Airbnb host with thousands of
reviews) and :func:`infer_owner_type` falls back to phrases in the title and body.
Anything unrecognised becomes :attr:`OwnerType.UNKNOWN`, never an error, and a person
can correct the result on the property (``Property.owner_type_override``).

Only the resulting type is kept. The host, agent and broker blocks that adapters read
signals from stay out of ``raw`` because they name people.
"""

from __future__ import annotations

import re
from enum import StrEnum


class OwnerType(StrEnum):
    INDIVIDUAL = "individual"
    PROPERTY_MANAGER = "property_manager"
    AGENT = "agent"
    UNKNOWN = "unknown"


_LABELS: dict[OwnerType, str] = {
    OwnerType.INDIVIDUAL: "Private landlord",
    OwnerType.PROPERTY_MANAGER: "Property manager",
    OwnerType.AGENT: "Agent / broker",
    OwnerType.UNKNOWN: "Unknown",
}


def owner_type_label(kind: OwnerType) -> str:
    """The wording the dashboard, the API docs and Discord use for ``kind``."""
    return _LABELS[kind]


_ALIASES: dict[str, OwnerType] = {
    **dict.fromkeys(
        (
            "individual",
            "landlord",
            "owner",
            "private landlord",
            "private owner",
            "independent landlord",
            "by owner",
            "for rent by owner",
            "frbo",
        ),
        OwnerType.INDIVIDUAL,
    ),
    **dict.fromkeys(
        (
            "property manager",
            "property management",
            "property management company",
            "management",
            "management company",
            "manager",
            "company",
            "corporation",
            "corporate",
            "complex",
            "apartment complex",
        ),
        OwnerType.PROPERTY_MANAGER,
    ),
    **dict.fromkeys(
        ("agent", "broker", "brokerage", "realtor", "real estate agent", "listing agent"),
        OwnerType.AGENT,
    ),
    "unknown": OwnerType.UNKNOWN,
}


def _key(raw: str) -> str:
    # "Property-Manager" → "property manager"; enum values ("property_manager") match too.
    return " ".join(part for part in re.split(r"[\s\-_/.]+", raw.lower()) if part)


def normalize_owner_type(raw: str | OwnerType | None) -> OwnerType:
    """Map a label (case, spacing and hyphens ignored) to an :class:`OwnerType`."""
    if isinstance(raw, OwnerType):
        return raw
    if not raw:
        return OwnerType.UNKNOWN
    return _ALIASES.get(_key(raw), OwnerType.UNKNOWN)


def _patterns(*patterns: str, flags: int = re.IGNORECASE) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, flags) for pattern in patterns)


# First-person ownership claims. Checked first: "no brokers" is an individual's phrase
# even though the agent table would match "broker".
_INDIVIDUAL = _patterns(
    r"\bprivate\s+(?:landlord|owner)\b",
    r"\bby\s+owner\b",
    r"\bI\s+am\s+the\s+(?:owner|landlord)\b",
    r"\bno\s+brokers?\b",
    r"\bowner[-\s]occupied\b",
    r"\bmy\s+(?:home|house|place|apartment|condo)\b",
    r"\bwe\s+live\b",
)

# A profession named in the text.
_AGENT = _patterns(
    r"\bbrokers?\b",
    r"\brealtors?\b",
    r"\blisting\s+agent\b",
    r"\bbrokerage\b",
    r"\bbroker'?s?\s+fee\b",
)

# The vocabulary of leasing offices. Broadest, so last: a private landlord can charge an
# application fee too.
_PROPERTY_MANAGER = _patterns(
    r"\bproperty\s+management\b",
    r"\bmanagement\s+company\b",
    r"\bleasing\s+(?:office|consultant|team)\b",
    r"\bapply\s+online\b",
    r"\bapplication\s+fee\b",
    r"\bour\s+(?:[\w-]+\s+)?community\b",
    r"\bresident\s+portal\b",
    r"\bon[-\s]?site\s+management\b",
    r"\bprofessionally\s+managed\b",
    r"\boffice\s+hours\b",
)
# Company-name markers, case-sensitive: "Example Properties LLC", but not "properties
# available" or a sentence that starts with "Properties".
_PROPERTY_MANAGER_CASED = _patterns(
    r"\bLLC\b", r"\bInc\.", r"\b[A-Z][\w&'-]*\s+Properties\b", flags=0
)

_TABLES: tuple[tuple[OwnerType, tuple[re.Pattern[str], ...]], ...] = (
    (OwnerType.INDIVIDUAL, _INDIVIDUAL),
    (OwnerType.AGENT, _AGENT),
    (OwnerType.PROPERTY_MANAGER, _PROPERTY_MANAGER + _PROPERTY_MANAGER_CASED),
)


def infer_owner_type(title: str, description: str, *, hint: OwnerType | None = None) -> OwnerType:
    """The owner type of a listing from its adapter's ``hint`` and its text.

    A hint other than :attr:`OwnerType.UNKNOWN` wins: it comes from the source's own
    record of the listing, while the text is a guess. Otherwise the title and
    description are scanned against the phrase tables in order (individual, agent,
    property manager) and the first table with a hit decides. No hit is ``UNKNOWN``.
    """
    if hint is not None and hint is not OwnerType.UNKNOWN:
        return hint
    text = f"{title}\n{description}"
    for kind, patterns in _TABLES:
        if any(pattern.search(text) for pattern in patterns):
            return kind
    return OwnerType.UNKNOWN
