"""The ``enrich.risk`` job: signs that a listing may be a scam.

Approving a property queues this job (``SqlPropertyStore.transition``, in the same
transaction). It runs three independent checks on the property's primary listing and
stores each hit as a ``risk_flags`` row (kind, severity, detail):

* ``price_below_market``: the monthly price is at least ``RISK_PRICE_BELOW_MEDIAN_PCT``
  percent under the median of comparable listings (other active listings with the
  same bedroom count and currency within ``RISK_COMPARABLE_RADIUS_KM``, one per
  property). Needs ``RISK_MIN_COMPARABLES`` of them. Twice the threshold is ``high``.
* ``photo_reuse``: a photo of the property is, by pHash (``RISK_PHASH_DISTANCE``), the
  same as a photo of a listing at a *different street address*. The same unit on
  other sources shares its address and is not flagged. Always ``high``.
* ``off_platform_payment``: the title or description asks for a wire transfer, gift
  cards, cryptocurrency, mailed keys, or Zelle-style payment before viewing. ``high``
  when it asks for something that cannot be recovered, else ``medium``.

Re-running updates flags in place and removes those that no longer apply, so a retry
needs no cleanup. ``RISK_ENABLED=false`` makes the job a no-op.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import RiskSettings
from furnest.db.models import Listing, ListingImage, RiskFlag
from furnest.db.property_store import ENRICH_RISK, primary_listing
from furnest.db.session import session_scope
from furnest.dedup.phash import hamming
from furnest.domain import DistanceUnit, NormalizedListing, haversine
from furnest.enrichment.alternatives import load_enrichable
from furnest.logging import get_logger

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext, JobRegistry

log = get_logger(__name__)

PRICE_BELOW_MARKET = "price_below_market"
PHOTO_REUSE = "photo_reuse"
OFF_PLATFORM_PAYMENT = "off_platform_payment"
LOW, MEDIUM, HIGH = "low", "medium", "high"

_VIEWING = r"(?:view(?:ing)?|visit|see(?:ing)?|tour|showing|inspect(?:ion)?)"
_APP = r"(?:zelle|venmo|cash\s?app|paypal)"
# (label, severity, pattern). The severity is "high" for money that cannot be recovered.
_PAYMENT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (label, severity, re.compile(pattern, re.IGNORECASE))
    for label, severity, pattern in (
        ("wire transfer", HIGH, r"\bwire(?:d)?\s+(?:transfer|the|money|funds|payment|deposit)"),
        ("wire transfer", HIGH, r"\b(?:western\s+union|money\s?gram)\b"),
        ("gift card", HIGH, r"\bgift\s+cards?\b"),
        ("cryptocurrency", HIGH, r"\b(?:bitcoin|btc|crypto(?:currency)?|usdt)\b"),
        ("keys sent by mail", HIGH, r"\bkeys?\b.{0,30}\b(?:mailed|shipped|couriered|sent)\b"),
        (
            "payment app before viewing",
            MEDIUM,
            rf"\b{_APP}\b.{{0,80}}\b(?:before|prior\s+to|without)\b.{{0,40}}\b{_VIEWING}\b",
        ),
        (
            "payment app before viewing",
            MEDIUM,
            rf"\b(?:before|prior\s+to|without)\b.{{0,40}}\b{_VIEWING}\b.{{0,80}}\b{_APP}\b",
        ),
        (
            "deposit before viewing",
            MEDIUM,
            rf"\bdeposit\b.{{0,40}}\b(?:before|prior\s+to|without)\b.{{0,40}}\b{_VIEWING}\b",
        ),
    )
)


@dataclass(frozen=True)
class Flag:
    kind: str
    severity: str
    summary: str
    detail: dict[str, Any]


def price_flag(price_minor: int, comparables: list[int], settings: RiskSettings) -> Flag | None:
    """A flag when ``price_minor`` is far below the median of ``comparables``."""
    if len(comparables) < settings.min_comparables:
        return None
    median = statistics.median(comparables)
    if median <= 0:
        return None
    below = (median - price_minor) * 100 / median
    if below < settings.price_below_median_pct:
        return None
    severity = HIGH if below >= settings.price_below_median_pct * 2 else MEDIUM
    return Flag(
        PRICE_BELOW_MARKET,
        severity,
        f"Price is {below:.0f}% below the median of {len(comparables)} nearby listings",
        {
            "price_minor": price_minor,
            "median_minor": round(median),
            "percent_below": round(below, 1),
            "comparables": len(comparables),
        },
    )


def reused_photos(
    own: list[str], others: list[tuple[int, str]], max_distance: int
) -> dict[int, int]:
    """Listing id → number of own photos matching one of its ``(listing_id, phash)`` photos."""
    found: dict[int, set[str]] = {}
    for listing_id, other in others:
        for mine in own:
            if hamming(mine, other) <= max_distance:
                found.setdefault(listing_id, set()).add(mine)
    return {listing_id: len(hashes) for listing_id, hashes in found.items()}


def payment_flag(text: str) -> Flag | None:
    """A flag when ``text`` uses off-platform payment language."""
    labels: list[str] = []
    severity = MEDIUM
    for label, level, pattern in _PAYMENT_PATTERNS:
        if pattern.search(text):
            if label not in labels:
                labels.append(label)
            if level == HIGH:
                severity = HIGH
    if not labels:
        return None
    return Flag(
        OFF_PLATFORM_PAYMENT,
        severity,
        "Listing text mentions off-platform payment: " + ", ".join(labels),
        {"matches": labels},
    )


class CheckRisk:
    def __init__(self, session_factory: sessionmaker[Session], settings: RiskSettings) -> None:
        self._factory = session_factory
        self._settings = settings

    def __call__(self, ctx: JobContext) -> None:
        self.run(int(ctx.payload["property_id"]))

    def run(self, property_id: int) -> list[Flag]:
        """Evaluate and store the flags of one property; returns the ones raised."""
        if not self._settings.enabled:
            return []
        with session_scope(self._factory) as session:
            if load_enrichable(session, property_id) is None:
                log.info("risk_skipped", property_id=property_id, reason="not enrichable")
                return []
            primary = primary_listing(session, property_id)
            if primary is None:
                return []
            flags = [
                flag
                for flag in (
                    self._price(session, primary),
                    self._photos(session, property_id, primary),
                    payment_flag(self._text(primary)),
                )
                if flag is not None
            ]
            self._store(session, property_id, flags)
        log.info("risk_checked", property_id=property_id, flags=[flag.kind for flag in flags])
        return flags

    @staticmethod
    def _text(primary: Listing) -> str:
        description = NormalizedListing.model_validate(primary.data).description
        return f"{primary.title}\n{description}"

    def _price(self, session: Session, primary: Listing) -> Flag | None:
        if (
            primary.monthly_price_minor is None
            or primary.currency is None
            or primary.beds is None
            or primary.lat is None
            or primary.lng is None
        ):
            return None
        radius = self._settings.comparable_radius_km
        # One degree of latitude is about 111 km; widen longitude by the latitude's cosine.
        dlat = radius / 111.0
        dlng = radius / (111.0 * max(0.01, abs(math.cos(math.radians(primary.lat)))))
        rows = session.execute(
            select(
                Listing.id,
                Listing.property_id,
                Listing.monthly_price_minor,
                Listing.lat,
                Listing.lng,
            ).where(
                Listing.removed_at.is_(None),
                Listing.beds == primary.beds,
                Listing.currency == primary.currency,
                Listing.monthly_price_minor.is_not(None),
                Listing.lat.between(primary.lat - dlat, primary.lat + dlat),
                Listing.lng.between(primary.lng - dlng, primary.lng + dlng),
            )
        )
        own_property = primary.property_id
        prices: dict[tuple[str, int], int] = {}
        for listing_id, property_id, price, lat, lng in rows:
            if property_id is not None and property_id == own_property:
                continue
            if price is None or price <= 0 or lat is None or lng is None:
                continue
            if haversine(primary.lat, primary.lng, lat, lng, DistanceUnit.KILOMETERS) > radius:
                continue
            # One price per property: the same unit on several sources counts once.
            key = ("p", property_id) if property_id is not None else ("l", listing_id)
            prices.setdefault(key, price)
        return price_flag(primary.monthly_price_minor, list(prices.values()), self._settings)

    def _photos(self, session: Session, property_id: int, primary: Listing) -> Flag | None:
        own_listings = list(
            session.scalars(select(Listing).where(Listing.property_id == property_id))
        )
        own_streets = {item.street_key for item in own_listings if item.street_key}
        hashes = session.scalars(
            select(ListingImage.phash).where(
                ListingImage.listing_id.in_([item.id for item in own_listings]),
                ListingImage.phash.is_not(None),
            )
        )
        own = sorted({phash for phash in hashes if phash is not None})
        if not own or not own_streets:
            return None
        rows = session.execute(
            select(ListingImage.listing_id, ListingImage.phash, Listing.street_key)
            .join(Listing, Listing.id == ListingImage.listing_id)
            .where(
                ListingImage.phash.is_not(None),
                Listing.street_key.is_not(None),
                (Listing.property_id.is_(None)) | (Listing.property_id != property_id),
            )
        )
        streets: dict[int, str] = {}
        others: list[tuple[int, str]] = []
        for listing_id, phash, street in rows:
            if street in own_streets or phash is None or street is None:
                continue
            streets[listing_id] = street
            others.append((listing_id, phash))
        matches = reused_photos(own, others, self._settings.phash_distance)
        if not matches:
            return None
        elsewhere = sorted({streets[listing_id] for listing_id in matches})
        return Flag(
            PHOTO_REUSE,
            HIGH,
            f"Photos also appear in listings at {len(elsewhere)} other address(es)",
            {
                "listing_ids": sorted(matches),
                "photos_matched": max(matches.values()),
                "other_addresses": elsewhere[:5],
                "primary_listing_id": primary.id,
            },
        )

    @staticmethod
    def _store(session: Session, property_id: int, flags: list[Flag]) -> None:
        existing = {
            row.kind: row
            for row in session.scalars(select(RiskFlag).where(RiskFlag.property_id == property_id))
        }
        for flag in flags:
            row = existing.pop(flag.kind, None)
            if row is None:
                row = RiskFlag(property_id=property_id, kind=flag.kind)
                session.add(row)
            row.severity = flag.severity
            row.summary = flag.summary
            row.detail = flag.detail
        for row in existing.values():
            session.delete(row)


def register_risk(
    jobs: JobRegistry, session_factory: sessionmaker[Session], settings: RiskSettings
) -> None:
    """Register :class:`CheckRisk` as the ``enrich.risk`` job."""
    jobs.register(ENRICH_RISK, CheckRisk(session_factory, settings), timeout=timedelta(seconds=120))
