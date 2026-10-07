"""Deciding whether two listings are the same unit.

The same apartment often shows up on several sources, each with its own wording,
photo crop and address formatting. :func:`score_pair` compares two listings'
:class:`ListingFacts` and decides whether they should be grouped under one property.
It is pure: the grouping store (:mod:`furnest.db.grouping`) gathers the facts,
finds the candidates and applies the decision.

**Vetoes** come first. Two listings are never the same unit when:

* both give a bedroom count and the counts differ. Zillow lists a building as one
  listing per bedroom group, all sharing the building's address, coordinates and
  photos (see ``docs/adapters.md``), so everything else would say "same".
* both addresses name a unit and the units differ.
* both give coordinates more than ``max_distance_m`` apart.

**Signals** each score 0 to 1:

* ``address``: 1 for the same canonical address, 0.6 for the same street address
  with a unit on one side only, else 0.
* ``distance``: 1 at the same point, falling to 0 at ``max_distance_m``.
* ``photos``: the share of the smaller photo set that has a near-identical photo
  (pHash) in the other.
* ``title`` and ``description``: token-set similarity (:mod:`furnest.dedup.text`).

A signal is only counted when both listings have what it needs, so a missing photo
hash or coordinate neither helps nor hurts. The score is the weighted mean of the
counted signals, and the pair matches when at least ``min_signals`` were counted
and the score reaches ``match_threshold``.

A match also needs an **anchor**: the same street address, or at least half the
photos in common. Text and distance alone aren't enough, because a property manager
reuses one ad for every unit in a building.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from furnest.dedup.phash import matching_pairs
from furnest.dedup.text import text_similarity
from furnest.domain import DistanceUnit, ParsedAddress, haversine

if TYPE_CHECKING:
    from furnest.config import GroupingSettings

# The address score for the same street address where only one side names a unit.
STREET_MATCH = 0.6
# The share of photos in common that anchors a match on its own.
PHOTO_ANCHOR = 0.5


@dataclass(frozen=True)
class GroupingThresholds:
    """The knobs of :func:`score_pair`; :meth:`from_settings` reads ``GROUPING_*``."""

    max_distance_m: float = 75.0
    match_threshold: float = 0.75
    min_signals: int = 2
    phash_distance: int = 8
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "address": 3.0,
            "distance": 1.0,
            "photos": 3.0,
            "title": 1.0,
            "description": 1.0,
        }
    )

    @classmethod
    def from_settings(cls, settings: GroupingSettings) -> GroupingThresholds:
        return cls(
            max_distance_m=settings.max_distance_m,
            match_threshold=settings.match_threshold,
            min_signals=settings.min_signals,
            phash_distance=settings.phash_distance,
            weights={
                "address": settings.weight_address,
                "distance": settings.weight_distance,
                "photos": settings.weight_photos,
                "title": settings.weight_title,
                "description": settings.weight_description,
            },
        )


@dataclass(frozen=True)
class AddressKey:
    """The parts of a parsed address grouping compares."""

    # The whole canonical line, unit included.
    canonical: str
    # Number, street and ZIP (or city) without the unit; None unless the parser
    # found a house number and street.
    street: str | None
    unit: str | None

    @classmethod
    def of(cls, parsed: ParsedAddress) -> AddressKey | None:
        """The key of ``parsed``, or None unless it has a house number and street (or
        a PO box): a street or city alone names many units."""
        if not ((parsed.house_number and parsed.street) or parsed.po_box):
            return None
        street = None
        if parsed.house_number and parsed.street:
            place = parsed.postal_code or parsed.city
            if place:
                street = f"{parsed.house_number} {parsed.street}, {place}".lower()
        return cls(canonical=parsed.canonical, street=street, unit=parsed.unit)

    @classmethod
    def stored(cls, canonical: str | None, street: str | None) -> AddressKey | None:
        """The key read back from its stored ``canonical`` and ``street`` columns.

        The unit is the ``#unit`` the canonical line ends its street part with
        (:func:`~furnest.domain.address.normalize_unit` writes units that way).
        """
        if canonical is None:
            return None
        line = canonical.split(",", 1)[0]
        unit = line[line.rindex("#") :] if "#" in line else None
        return cls(canonical=canonical, street=street, unit=unit)


@dataclass(frozen=True)
class ListingFacts:
    """What grouping knows about one listing."""

    listing_id: int
    address: AddressKey | None = None
    lat: float | None = None
    lng: float | None = None
    beds: int | None = None
    phashes: tuple[str, ...] = ()
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class PairScore:
    """How alike two listings are, and why."""

    # Weighted mean of ``signals``; 0 when vetoed or nothing could be compared.
    score: float
    # The counted signals, each 0 to 1.
    signals: dict[str, float]
    # Why the pair can't be the same unit, if it can't.
    veto: str | None
    matched: bool

    def as_payload(self) -> dict[str, object]:
        """For an audit event."""
        return {
            "score": round(self.score, 3),
            "signals": {name: round(value, 3) for name, value in self.signals.items()},
        }


def distance_m(a: ListingFacts, b: ListingFacts) -> float | None:
    """Meters between the two listings, or None unless both have coordinates."""
    if a.lat is None or a.lng is None or b.lat is None or b.lng is None:
        return None
    return haversine(a.lat, a.lng, b.lat, b.lng, DistanceUnit.KILOMETERS) * 1000


def _veto(a: ListingFacts, b: ListingFacts, distance: float | None, limit: float) -> str | None:
    if a.beds is not None and b.beds is not None and a.beds != b.beds:
        return "different bedroom counts"
    units = (a.address.unit if a.address else None, b.address.unit if b.address else None)
    if all(units) and units[0] != units[1]:
        return "different units"
    if distance is not None and distance > limit:
        return "too far apart"
    return None


def _address_signal(a: AddressKey | None, b: AddressKey | None) -> float | None:
    if a is None or b is None:
        return None
    if a.canonical == b.canonical:
        return 1.0
    if a.street is not None and a.street == b.street:
        return STREET_MATCH
    return 0.0


def _photo_signal(a: tuple[str, ...], b: tuple[str, ...], max_distance: int) -> float | None:
    if not a or not b:
        return None
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return matching_pairs(smaller, larger, max_distance=max_distance) / len(smaller)


def score_pair(a: ListingFacts, b: ListingFacts, thresholds: GroupingThresholds) -> PairScore:
    """Score two listings and decide whether they are the same unit."""
    distance = distance_m(a, b)
    veto = _veto(a, b, distance, thresholds.max_distance_m)
    if veto is not None:
        return PairScore(score=0.0, signals={}, veto=veto, matched=False)
    candidates: dict[str, float | None] = {
        "address": _address_signal(a.address, b.address),
        "distance": None if distance is None else 1 - distance / thresholds.max_distance_m,
        "photos": _photo_signal(a.phashes, b.phashes, thresholds.phash_distance),
        "title": text_similarity(a.title, b.title),
        "description": text_similarity(a.description, b.description),
    }
    signals = {
        name: value
        for name, value in candidates.items()
        if value is not None and thresholds.weights.get(name, 0) > 0
    }
    total = sum(thresholds.weights[name] for name in signals)
    weighted = sum(thresholds.weights[name] * value for name, value in signals.items())
    score = weighted / total if total else 0.0
    anchored = (
        signals.get("address", 0.0) >= STREET_MATCH or signals.get("photos", 0.0) >= PHOTO_ANCHOR
    )
    matched = (
        anchored and len(signals) >= thresholds.min_signals and score >= thresholds.match_threshold
    )
    return PairScore(score=score, signals=signals, veto=None, matched=matched)
