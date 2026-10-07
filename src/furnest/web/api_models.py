"""Request and response bodies of the JSON API (:mod:`furnest.web.routes.api`).

The store hands out frozen dataclasses. These Pydantic models are the API's
public shape and its OpenAPI schema, so a change to the store's internals doesn't
change the API by accident. Each ``*Out`` model has a ``build`` that converts
from the store's type.

Listings are returned as the adapter normalised them, but without ``raw`` (the
source's untouched payload, which is large and differs by source). Descriptions
are the scraped text as stored, not sanitised HTML: the API consumer decides how
to display them.
"""

from datetime import date
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from furnest.db.listing_store import PricePoint
from furnest.db.property_store import (
    MAX_NOTE_LENGTH,
    AuditEntry,
    ListingDetail,
    ListingSummary,
    NoteEntry,
    PropertyDetail,
    PropertyPage,
    PropertyPricePoint,
    PropertyState,
)
from furnest.domain import Actor, Money, PropertyStatus
from furnest.domain.listing import Address, Contact
from furnest.domain.price import Price
from furnest.domain.property_type import PropertyType
from furnest.domain.status import allowed_targets
from furnest.matching import MatchResult

# Request bodies may carry some whitespace around a note of MAX_NOTE_LENGTH; the
# store enforces the limit on the stripped text.
_BODY_MAX = MAX_NOTE_LENGTH + 1_000


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class PropertyOut(_Out):
    id: int
    status: PropertyStatus
    address: str | None
    lat: float | None
    lng: float | None
    # 0-100; None until the property's listing has been matched.
    score: int | None
    criteria_version_id: int | None
    snoozed_until: AwareDatetime | None
    manual_contact_required: bool
    status_changed_at: AwareDatetime
    created_at: AwareDatetime
    allowed_transitions: list[PropertyStatus] = Field(
        description="Every status POST /transitions accepts for this property right now."
    )
    merged_into_id: int | None = Field(
        default=None,
        description="Set when this property turned out to be the same unit as another and was "
        "merged into it; its listings, notes and emails are there now.",
    )

    @classmethod
    def build(cls, state: PropertyState) -> Self:
        return cls(
            id=state.id,
            status=state.status,
            address=state.address,
            lat=state.lat,
            lng=state.lng,
            score=state.score,
            criteria_version_id=state.criteria_version_id,
            snoozed_until=state.snoozed_until,
            manual_contact_required=state.manual_contact_required,
            status_changed_at=state.status_changed_at,
            created_at=state.created_at,
            allowed_transitions=(
                []
                if state.merged_into_id is not None
                else list(allowed_targets(state.status, Actor.API))
            ),
            merged_into_id=state.merged_into_id,
        )


class ListingSummaryOut(_Out):
    id: int
    source: str
    url: str
    title: str
    monthly_price: Money | None
    beds: int | None
    baths: float | None
    # Set once the source stopped showing the listing.
    removed_at: AwareDatetime | None

    @classmethod
    def build(cls, summary: ListingSummary) -> Self:
        return cls(
            id=summary.id,
            source=summary.source,
            url=summary.url,
            title=summary.title,
            monthly_price=summary.monthly_price,
            beds=summary.beds,
            baths=summary.baths,
            removed_at=summary.removed_at,
        )


class PropertyListItem(_Out):
    property: PropertyOut
    # The primary listing (active first); None only if all were deleted.
    listing: ListingSummaryOut | None


class PropertyPageOut(_Out):
    items: list[PropertyListItem]
    # Matching properties across all pages.
    total: int
    limit: int
    offset: int

    @classmethod
    def build(cls, page: PropertyPage, *, limit: int, offset: int) -> Self:
        return cls(
            items=[
                PropertyListItem(
                    property=PropertyOut.build(row.property),
                    listing=None if row.listing is None else ListingSummaryOut.build(row.listing),
                )
                for row in page.rows
            ],
            total=page.total,
            limit=limit,
            offset=offset,
        )


class PricePointOut(_Out):
    price: Price
    observed_at: AwareDatetime

    @classmethod
    def build(cls, point: PricePoint) -> Self:
        return cls(price=point.price, observed_at=point.observed_at)


class PropertyPricePointOut(PricePointOut):
    """One price change of one of the property's listings."""

    listing_id: int
    source: str

    @classmethod
    def build_property(cls, point: PropertyPricePoint) -> Self:
        return cls(
            listing_id=point.listing_id,
            source=point.source,
            price=point.price,
            observed_at=point.observed_at,
        )


class ListingOut(ListingSummaryOut):
    """One listing in full, as its source adapter normalised it."""

    source_id: str
    description: str
    price: Price | None
    address: Address
    lat: float | None
    lng: float | None
    sqft: int | None
    property_type: PropertyType
    furnished: bool | None
    amenities: list[str]
    available_from: date | None
    min_lease_months: int | None
    max_lease_months: int | None
    images: list[HttpUrl]
    contact: Contact
    first_seen: AwareDatetime
    last_seen: AwareDatetime
    # Oldest first.
    price_history: list[PricePointOut]

    @classmethod
    def build_detail(cls, detail: ListingDetail) -> Self:
        listing = detail.listing
        summary = ListingSummaryOut.build(detail.summary)
        return cls(
            **summary.model_dump(),
            source_id=listing.source_id,
            description=listing.description,
            price=listing.price,
            address=listing.address,
            lat=listing.lat,
            lng=listing.lng,
            sqft=listing.sqft,
            property_type=listing.property_type,
            furnished=listing.furnished,
            amenities=sorted(listing.amenities),
            available_from=listing.available_from,
            min_lease_months=listing.min_lease_months,
            max_lease_months=listing.max_lease_months,
            images=list(listing.images),
            contact=listing.contact,
            first_seen=listing.first_seen,
            last_seen=listing.last_seen,
            price_history=[PricePointOut.build(point) for point in detail.price_history],
        )


class AuditEntryOut(_Out):
    id: int
    # None on the event that created the property.
    from_status: PropertyStatus | None
    to_status: PropertyStatus
    actor: Actor
    actor_detail: str | None
    payload: dict[str, object] | None
    created_at: AwareDatetime

    @classmethod
    def build(cls, entry: AuditEntry) -> Self:
        return cls(
            id=entry.id,
            from_status=entry.from_status,
            to_status=entry.to_status,
            actor=entry.actor,
            actor_detail=entry.actor_detail,
            payload=entry.payload,
            created_at=entry.created_at,
        )


class NoteOut(_Out):
    id: int
    property_id: int
    # Markdown, unrendered.
    body_md: str
    actor: Actor
    actor_detail: str | None
    created_at: AwareDatetime

    @classmethod
    def build(cls, note: NoteEntry) -> Self:
        return cls(
            id=note.id,
            property_id=note.property_id,
            body_md=note.body_md,
            actor=note.actor,
            actor_detail=note.actor_detail,
            created_at=note.created_at,
        )


class PropertyDetailOut(_Out):
    property: PropertyOut
    # None until the property's listing has been matched.
    match: MatchResult | None
    # Primary listing first.
    listings: list[ListingOut]
    # Oldest first.
    audit: list[AuditEntryOut]
    # Oldest first.
    notes: list[NoteOut]
    # Every listing's price changes in one timeline, oldest first.
    price_history: list[PropertyPricePointOut]

    @classmethod
    def build(cls, detail: PropertyDetail) -> Self:
        return cls(
            property=PropertyOut.build(detail.property),
            match=detail.match,
            listings=[ListingOut.build_detail(listing) for listing in detail.listings],
            audit=[AuditEntryOut.build(entry) for entry in detail.audit],
            notes=[NoteOut.build(note) for note in detail.notes],
            price_history=[
                PropertyPricePointOut.build_property(point) for point in detail.price_history
            ],
        )


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransitionIn(_In):
    to: PropertyStatus
    reason: str | None = Field(
        default=None,
        max_length=_BODY_MAX,
        description="Only with to=denied: saved as a markdown note on the property.",
    )


class NoteIn(_In):
    body: str = Field(max_length=_BODY_MAX, description="Markdown.")
