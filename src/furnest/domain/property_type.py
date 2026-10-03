"""Property types and the mapping from each source's wording to them.

Sources use dozens of labels for the same kind of place ("apt", "flat", "SFH",
"townhome"). Adapters pass whatever they scraped through
:func:`normalize_property_type`. Anything unrecognised becomes
:attr:`PropertyType.OTHER` rather than an error, so a new label never drops a listing.
"""

from __future__ import annotations

import re
from enum import StrEnum


class PropertyType(StrEnum):
    APARTMENT = "apartment"
    TOWNHOUSE = "townhouse"
    SINGLE_FAMILY = "single_family"
    CONDO = "condo"
    ROOM = "room"
    STUDIO = "studio"
    OTHER = "other"


_ALIASES: dict[str, PropertyType] = {
    **dict.fromkeys(
        ("apartment", "apt", "apartments", "flat", "unit", "duplex", "triplex", "loft"),
        PropertyType.APARTMENT,
    ),
    **dict.fromkeys(
        ("townhouse", "townhome", "town house", "town home", "rowhouse", "row house"),
        PropertyType.TOWNHOUSE,
    ),
    **dict.fromkeys(
        (
            "single family",
            "single family home",
            "single family house",
            "single family residence",
            "sfh",
            "sfr",
            "house",
            "home",
            "detached",
            "cottage",
            "bungalow",
            "cabin",
        ),
        PropertyType.SINGLE_FAMILY,
    ),
    **dict.fromkeys(("condo", "condominium", "co op", "coop"), PropertyType.CONDO),
    **dict.fromkeys(
        (
            "room",
            "private room",
            "shared room",
            "shared",
            "room share",
            "roommate",
            "bedroom",
        ),
        PropertyType.ROOM,
    ),
    **dict.fromkeys(
        ("studio", "studio apartment", "efficiency", "bachelor", "micro unit"),
        PropertyType.STUDIO,
    ),
}


def _key(raw: str) -> str:
    # "Single-Family_Home" → "single family home"; enum values ("single_family") match too.
    return " ".join(part for part in re.split(r"[\s\-_/.]+", raw.lower()) if part)


def normalize_property_type(raw: str | PropertyType | None) -> PropertyType:
    """Map a source label (case, spacing and hyphens ignored) to a :class:`PropertyType`."""
    if isinstance(raw, PropertyType):
        return raw
    if not raw:
        return PropertyType.OTHER
    return _ALIASES.get(_key(raw), PropertyType.OTHER)
