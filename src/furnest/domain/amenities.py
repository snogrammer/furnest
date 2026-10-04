"""Amenity tags: the one vocabulary every extractor emits, and the extractor interface.

Listings describe amenities in free text ("W/D in unit", "central A/C", "sorry, no
dogs") and adapters pass through whatever labels the source uses. An
:class:`AmenityExtractor` reads a :class:`~furnest.domain.listing.NormalizedListing`
and returns an :class:`AmenityResult` in terms of :class:`Amenity` tags, so criteria
(``must_have_amenities``, ``nice_to_have_amenities``) and listings speak the same
words.

The result keeps three sets apart. ``present`` tags were stated, ``absent`` tags were
explicitly ruled out ("no pets"), and anything in neither is unknown. Hard filters
(#16) treat a known "no" differently from silence, so collapsing absent into unknown
would lose information. ``unrecognized`` holds adapter labels no tag covered, for
logging and for growing the vocabulary.

The deterministic keyword extractor lives in :mod:`furnest.matching.amenities`; an
LLM extractor can implement the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from furnest.domain.listing import NormalizedListing


class Amenity(StrEnum):
    KING_BED = "king_bed"
    QUEEN_BED = "queen_bed"
    DECK_PATIO = "deck_patio"
    BALCONY = "balcony"
    FREE_PARKING = "free_parking"
    GARAGE = "garage"
    IN_UNIT_LAUNDRY = "in_unit_laundry"
    SHARED_LAUNDRY = "shared_laundry"
    PETS_ALLOWED = "pets_allowed"
    CATS_ALLOWED = "cats_allowed"
    DOGS_ALLOWED = "dogs_allowed"
    AC = "ac"
    HEATING = "heating"
    DISHWASHER = "dishwasher"
    WIFI = "wifi"
    UTILITIES_INCLUDED = "utilities_included"
    POOL = "pool"
    GYM = "gym"
    ELEVATOR = "elevator"
    WHEELCHAIR_ACCESSIBLE = "wheelchair_accessible"
    FIREPLACE = "fireplace"
    EV_CHARGING = "ev_charging"
    WORKSPACE = "workspace"
    SMOKE_FREE = "smoke_free"


@dataclass(frozen=True, slots=True)
class AmenityResult:
    present: frozenset[Amenity] = frozenset()
    absent: frozenset[Amenity] = frozenset()
    unrecognized: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if overlap := self.present & self.absent:
            raise ValueError(f"amenities both present and absent: {sorted(overlap)}")

    def status(self, tag: str) -> bool | None:
        """``True`` if present, ``False`` if ruled out, ``None`` if unknown."""
        if tag in self.present:
            return True
        if tag in self.absent:
            return False
        return None


class AmenityExtractor(Protocol):
    def extract(self, listing: NormalizedListing) -> AmenityResult: ...
