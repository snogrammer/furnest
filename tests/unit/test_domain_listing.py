"""NormalizedListing: required fields, validation rules and JSON round-trip."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from furnest.domain import (
    Address,
    Contact,
    Money,
    NormalizedListing,
    Price,
    PropertyType,
    RentPeriod,
)

SEEN = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def minimal(**overrides: object) -> NormalizedListing:
    fields: dict[str, Any] = {
        "source": "example",
        "source_id": "abc-123",
        "url": "https://listings.example.com/abc-123",
        "title": "Furnished 2BR near the park",
    }
    fields.update(overrides)
    return NormalizedListing.model_validate(fields)


def full() -> NormalizedListing:
    return minimal(
        description="Bright corner unit.",
        price=Price(amount=Money(amount_minor=60000), period=RentPeriod.WEEKLY),
        address=Address(
            line1="123 Example St",
            line2="Apt 4",
            city="Springfield",
            region="ST",
            postal_code="00000",
            country="us",
            neighborhood="Downtown",
        ),
        lat=40.0,
        lng=-105.0,
        beds=2,
        baths=1.5,
        sqft=900,
        property_type="Apt",
        furnished=True,
        amenities=["In_Unit_Laundry", " ac ", "", "AC"],
        available_from=date(2026, 11, 1),
        min_lease_months=1,
        max_lease_months=6,
        images=["https://img.example.com/1.jpg"],
        contact=Contact(name="Leasing Office", email="leasing@example.com", phone="555-0100"),
        raw={"id": "abc-123", "price": "$600/wk", "tags": ["a", 1, None]},
        first_seen=SEEN,
        last_seen=SEEN + timedelta(hours=1),
    )


def test_minimal_listing_has_unknowns() -> None:
    listing = minimal()
    assert listing.price is None
    assert listing.monthly_price is None
    assert listing.property_type is PropertyType.OTHER
    assert listing.furnished is None
    assert listing.amenities == frozenset()
    assert listing.address.country == "US"
    assert listing.contact.platform_only is False
    assert listing.first_seen.tzinfo is not None
    assert listing.first_seen <= listing.last_seen


def test_full_listing_is_normalized() -> None:
    listing = full()
    assert listing.monthly_price == Money(amount_minor=260000)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.amenities == frozenset({"in_unit_laundry", "ac"})
    assert listing.address.country == "US"


def test_json_round_trip() -> None:
    listing = full()
    assert NormalizedListing.model_validate_json(listing.model_dump_json()) == listing


def test_listing_is_frozen() -> None:
    with pytest.raises(ValidationError):
        minimal().title = "changed"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"lat": 40.0}, "lat and lng"),
        ({"lng": -105.0}, "lat and lng"),
        ({"lat": 91, "lng": 0}, "less than or equal to 90"),
        ({"min_lease_months": 6, "max_lease_months": 3}, "min_lease_months"),
        ({"min_lease_months": 0}, "greater than or equal to 1"),
        ({"first_seen": SEEN, "last_seen": SEEN - timedelta(seconds=1)}, "first_seen"),
        ({"first_seen": datetime(2026, 10, 1), "last_seen": SEEN}, "timezone"),
        ({"baths": 1.25}, "multiple of 0.5"),
        ({"beds": -1}, "greater than or equal to 0"),
        ({"sqft": 0}, "greater than 0"),
        ({"title": "   "}, "at least 1 character"),
        ({"url": "not a url"}, "URL"),
        ({"amenities": "pool"}, "not a string"),
        ({"property_type": 3}, "must be a string"),
        ({"contact": {"email": "nope"}}, "email"),
        ({"unexpected": 1}, "Extra inputs"),
    ],
)
def test_invalid_listings_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        minimal(**overrides)


def test_price_range_and_studio() -> None:
    listing = minimal(
        beds=0,
        property_type="studio",
        price={
            "amount": {"amount_minor": 9500},
            "amount_max": {"amount_minor": 12000},
            "period": "nightly",
        },
    )
    assert listing.property_type is PropertyType.STUDIO
    assert listing.price is not None
    assert listing.price.monthly == Money(amount_minor=288958)
    assert listing.price.monthly_max == Money(amount_minor=365000)
