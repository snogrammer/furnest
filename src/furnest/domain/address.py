"""Parsed addresses: the interface every address parser implements, and its fallback.

Sources give addresses in whatever shape they like: one free-text line, separate
components, abbreviations or not. An :class:`AddressParser` turns a listing's
:class:`~furnest.domain.listing.Address` into a :class:`ParsedAddress` with
normalized components and a :attr:`ParsedAddress.canonical` string, so that two
spellings of the same unit compare equal. Matching reads the ZIP and neighborhood
from it; dedup (P7) groups listings by the canonical string.

The US implementation (``usaddress``) lives in :mod:`furnest.dedup.address`, so the
domain stays free of the tagging model. :class:`PassthroughAddressParser` is the
locale-neutral fallback: it only cleans up what the source gave, and it is what the
US parser falls back to when tagging fails.

Normalization follows USPS conventions where they help comparison: street suffixes
and directionals are abbreviated (``Avenue`` → ``ave``, ``North`` → ``n``) and state
names become two-letter codes. Units collapse to ``#<id>`` whatever their
designator (``Apt 4B``, ``Unit 4b`` and ``#4B`` are all ``#4b``), because sources
use the designators interchangeably for the same door.
"""

from __future__ import annotations

import re
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints

from furnest.domain.listing import Address

OptionalStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None

DIRECTIONALS: dict[str, str] = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
    **{abbr: abbr for abbr in ("n", "s", "e", "w", "ne", "nw", "se", "sw")},
}

# A common subset of USPS Publication 28 street suffixes: spelling → abbreviation.
STREET_SUFFIXES: dict[str, str] = {
    "alley": "aly",
    "avenue": "ave",
    "av": "ave",
    "aven": "ave",
    "boulevard": "blvd",
    "boul": "blvd",
    "center": "ctr",
    "centre": "ctr",
    "circle": "cir",
    "court": "ct",
    "cove": "cv",
    "crescent": "cres",
    "crossing": "xing",
    "drive": "dr",
    "drv": "dr",
    "expressway": "expy",
    "freeway": "fwy",
    "grove": "grv",
    "heights": "hts",
    "highway": "hwy",
    "hiway": "hwy",
    "junction": "jct",
    "lane": "ln",
    "manor": "mnr",
    "parkway": "pkwy",
    "pky": "pkwy",
    "place": "pl",
    "plaza": "plz",
    "point": "pt",
    "ridge": "rdg",
    "road": "rd",
    "square": "sq",
    "street": "st",
    "str": "st",
    "terrace": "ter",
    "trail": "trl",
    "village": "vlg",
}

US_STATES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "puerto rico": "PR",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

_ZIP = re.compile(r"^(\d{5})(?:[-\s]?(\d{4}))?$")
_PUNCT = re.compile(r"[.,;]")
_SPACE = re.compile(r"\s+")


def clean(value: str | None) -> str | None:
    """Lower-case, drop ``.,;`` and collapse whitespace; ``None`` when nothing is left."""
    if value is None:
        return None
    text = _SPACE.sub(" ", _PUNCT.sub(" ", value)).strip().lower()
    return text or None


def split_zip(value: str | None, country: str = "US") -> tuple[str | None, str | None]:
    """A US ZIP or ZIP+4 as ``(zip5, plus4)``; other values come back stripped and upper-cased."""
    if value is None or not value.strip():
        return None, None
    text = value.strip().upper()
    if country.upper() == "US" and (m := _ZIP.match(text)):
        return m.group(1), m.group(2)
    return text, None


def normalize_zip(value: str | None, country: str = "US") -> str | None:
    """The comparable part of a postal code: the 5-digit ZIP for US ZIP+4 values."""
    return split_zip(value, country)[0]


def normalize_region(value: str | None, country: str = "US") -> str | None:
    """A US state name becomes its two-letter code; anything else is upper-cased."""
    text = clean(value)
    if text is None:
        return None
    if country.upper() == "US":
        return US_STATES.get(text, text).upper()
    return text.upper()


def normalize_directional(value: str | None) -> str | None:
    text = clean(value)
    return DIRECTIONALS.get(text, text) if text else None


def normalize_suffix(value: str | None) -> str | None:
    text = clean(value)
    return STREET_SUFFIXES.get(text, text) if text else None


def normalize_unit(designator: str | None, identifier: str | None) -> str | None:
    """``#<id>`` for any designated unit; a bare designator (``basement``) stays as is."""
    ident = clean((identifier or "").replace("#", " "))
    if ident:
        return f"#{ident.replace(' ', '')}"
    return clean((designator or "").replace("#", " "))


class ParsedAddress(BaseModel):
    """Normalized address components. Text fields are lower-case; region/country upper-case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    house_number: OptionalStr = None
    street: OptionalStr = None
    unit: OptionalStr = None
    po_box: OptionalStr = None
    city: OptionalStr = None
    region: OptionalStr = None
    postal_code: OptionalStr = None
    postal_code_ext: OptionalStr = None
    country: str = "US"
    neighborhood: OptionalStr = None
    # False when the parser fell back to the source's components untagged.
    parsed: bool = False

    @property
    def canonical(self) -> str:
        """One comparable line, e.g. ``123 n main st #4b, springfield, il 62701, us``."""
        if self.po_box:
            line = f"po box {self.po_box}"
        else:
            line = " ".join(p for p in (self.house_number, self.street, self.unit) if p)
        region_zip = " ".join(p for p in (self.region, self.postal_code) if p)
        parts = (line, self.city, region_zip, self.country)
        return ", ".join(p.lower() for p in parts if p)


class AddressParser(Protocol):
    def parse(self, address: Address) -> ParsedAddress: ...


class PassthroughAddressParser:
    """Cleans the source's own components without tagging; works for any country.

    ``line1`` becomes the street and ``line2`` the unit, so the canonical string is
    still stable across case, punctuation and spacing differences.
    """

    def parse(self, address: Address) -> ParsedAddress:
        country = address.country
        postal_code, ext = split_zip(address.postal_code, country)
        return ParsedAddress(
            street=clean(address.line1),
            unit=clean(address.line2),
            city=clean(address.city),
            region=normalize_region(address.region, country),
            postal_code=postal_code,
            postal_code_ext=ext,
            country=country,
            neighborhood=clean(address.neighborhood),
            parsed=False,
        )
