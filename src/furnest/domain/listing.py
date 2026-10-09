"""NormalizedListing: the one listing shape every adapter produces and every consumer reads.

Adapters (P3) map whatever a source returns onto this model; ingestion, matching,
dedup and notifications only ever see a :class:`NormalizedListing`. It is a frozen
value object with no I/O. Fields a source doesn't provide stay ``None`` (unknown),
which matching treats differently from a known "no".

Validation is strict where a bad value would mislead later stages (coordinates out
of range, a lease minimum above its maximum, naive timestamps) and forgiving where
sources are messy: the property type and owner type accept any label and amenity
tags are lower-cased, so adapters can pass through what they scraped.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from furnest.domain.money import Money
from furnest.domain.owner_type import OwnerType, normalize_owner_type
from furnest.domain.price import Price
from furnest.domain.property_type import PropertyType, normalize_property_type

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
OptionalStr = Annotated[str, StringConstraints(strip_whitespace=True)] | None


def _now() -> datetime:
    return datetime.now(UTC)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Address(_Frozen):
    """Address components as the source gave them. Parsing to a canonical form is #13."""

    line1: OptionalStr = None
    line2: OptionalStr = None
    city: OptionalStr = None
    region: OptionalStr = None
    postal_code: OptionalStr = None
    country: Annotated[str, StringConstraints(to_upper=True, min_length=2, max_length=2)] = "US"
    neighborhood: OptionalStr = None


class Contact(_Frozen):
    """How to reach the lister. ``platform_only`` means messages must go through the source."""

    name: OptionalStr = None
    email: EmailStr | None = None
    phone: OptionalStr = None
    platform_only: bool = False


class NormalizedListing(_Frozen):
    source: NonEmptyStr
    source_id: NonEmptyStr
    url: HttpUrl
    title: NonEmptyStr
    description: str = ""

    price: Price | None = None

    address: Address = Field(default_factory=Address)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)

    beds: int | None = Field(default=None, ge=0, description="0 means studio")
    baths: float | None = Field(default=None, ge=0, multiple_of=0.5)
    sqft: int | None = Field(default=None, gt=0)
    property_type: PropertyType = PropertyType.OTHER
    # Who is behind the listing (#113): the adapter's inference, see domain/owner_type.py.
    owner_type: OwnerType = OwnerType.UNKNOWN
    furnished: bool | None = None
    amenities: frozenset[str] = frozenset()

    available_from: date | None = None
    min_lease_months: int | None = Field(default=None, ge=1)
    max_lease_months: int | None = Field(default=None, ge=1)

    images: tuple[HttpUrl, ...] = ()
    contact: Contact = Field(default_factory=Contact)

    raw: dict[str, JsonValue] = Field(default_factory=dict)
    first_seen: AwareDatetime = Field(default_factory=_now)
    last_seen: AwareDatetime = Field(default_factory=_now)

    @field_validator("property_type", mode="before")
    @classmethod
    def _normalize_property_type(cls, value: object) -> PropertyType:
        if value is None or isinstance(value, str):
            return normalize_property_type(value)
        raise ValueError("property_type must be a string")

    @field_validator("owner_type", mode="before")
    @classmethod
    def _normalize_owner_type(cls, value: object) -> OwnerType:
        if value is None or isinstance(value, str):
            return normalize_owner_type(value)
        raise ValueError("owner_type must be a string")

    @field_validator("amenities", mode="before")
    @classmethod
    def _normalize_amenities(cls, value: object) -> object:
        if isinstance(value, str):
            raise ValueError("amenities must be a collection of tags, not a string")
        if isinstance(value, list | tuple | set | frozenset):
            return frozenset(
                tag.strip().lower() for tag in value if isinstance(tag, str) and tag.strip()
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be given together")
        if (
            self.min_lease_months is not None
            and self.max_lease_months is not None
            and self.min_lease_months > self.max_lease_months
        ):
            raise ValueError("min_lease_months must not exceed max_lease_months")
        if self.first_seen > self.last_seen:
            raise ValueError("first_seen must not be after last_seen")
        return self

    @property
    def monthly_price(self) -> Money | None:
        """The monthly-equivalent (lower) price, or ``None`` when the price is unknown."""
        return self.price.monthly if self.price else None


def beds_baths(beds: int | None, baths: float | None) -> str | None:
    """A short "2 bd · 1.5 ba" summary ("Studio" for 0 beds), or ``None`` if both are unknown."""
    parts = []
    if beds is not None:
        parts.append("Studio" if beds == 0 else f"{beds} bd")
    if baths is not None:
        parts.append(f"{baths:g} ba")
    return " · ".join(parts) or None
