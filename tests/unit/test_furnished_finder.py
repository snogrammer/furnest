"""The Furnished Finder adapter: criteria → actor input, actor items → listings, and a
whole search against a faked Apify. Runs offline on tests/fixtures/furnished_finder."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import (
    Amenity,
    Criteria,
    Currency,
    Money,
    NormalizedListing,
    OwnerType,
    Price,
    PropertyType,
    register_currency,
)
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.furnished_finder import FurnishedFinder
from furnest.sources.adapters.furnished_finder.parse import build_input, parse_item, search_url
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "furnished_finder"
TODAY = date(2026, 10, 12)
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    return fixture("dataset_items.json")  # type: ignore[no-any-return]


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


# --- parse_item ----------------------------------------------------------------------


def test_an_entire_unit_maps_every_field() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert listing.source == "furnished_finder"
    assert listing.source_id == "10000001_1"
    assert str(listing.url) == "https://www.furnishedfinder.com/property/10000001"
    assert listing.title == "Example Building 1210"
    assert listing.description == (
        "One-bedroom apartment in a managed building with a gym and a rooftop deck.\n\n"
        "Walkable street with a grocery store nearby."
    )
    assert listing.price == Price(amount=usd(3257))
    assert (listing.address.city, listing.address.region, listing.address.country) == (
        "Denver",
        "Colorado",
        "US",
    )
    assert (listing.lat, listing.lng) == (39.74, -104.99)
    assert (listing.beds, listing.baths, listing.sqft) == (1, 1.0, 763)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.furnished is True
    assert listing.available_from == TODAY
    assert listing.min_lease_months == 1
    assert [str(url) for url in listing.images] == [
        "https://www.furnishedfinder.com/_pserp_/10000001/1/00000001.jpg",
        "https://www.furnishedfinder.com/_pserp_/10000001/1/00000002.jpg",
    ]
    assert listing.contact.platform_only is True
    assert (listing.contact.name, listing.contact.email, listing.contact.phone) == (
        None,
        None,
        None,
    )


def test_amenities_house_rules_and_laundry_become_words_the_extractor_reads() -> None:
    first, no_pets, _, in_complex, _ = (parse_item(item, today=TODAY) for item in items()[:5])

    assert {"air conditioning", "washer and dryer", "in-unit laundry", "pets allowed"} <= (
        first.amenities
    )
    assert {"no smoking", "utilities included"} <= first.amenities
    found = KeywordAmenityExtractor().extract(first)
    for tag in (
        Amenity.AC,
        Amenity.IN_UNIT_LAUNDRY,
        Amenity.PETS_ALLOWED,
        Amenity.SMOKE_FREE,
        Amenity.UTILITIES_INCLUDED,
        Amenity.WIFI,
    ):
        assert tag in found.present, tag

    assert "no pets" in no_pets.amenities
    assert Amenity.PETS_ALLOWED in KeywordAmenityExtractor().extract(no_pets).absent

    # "In Complex W/D" and "washer and dryer on premises" are shared, not in-unit.
    assert {"shared laundry", "washer and dryer on-site"} <= in_complex.amenities
    found = KeywordAmenityExtractor().extract(in_complex)
    assert Amenity.SHARED_LAUNDRY in found.present
    assert Amenity.IN_UNIT_LAUNDRY not in found.present


def test_the_landlords_pets_are_not_pets_allowed() -> None:
    item = {"listingId": "1_1", "name": "Place", "amenities": ["petsLiveOnThisProperty", "wifi"]}

    assert parse_item(item, today=TODAY).amenities == {"wifi"}


def test_raw_keeps_listing_facts_and_nothing_about_the_landlord() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert listing.raw["cleaningFee"] == "$150.00"
    assert listing.raw["houseRulesGraphql"] == items()[0]["houseRulesGraphql"]
    assert not any(key.startswith("landlord") for key in listing.raw)
    assert "reviews" not in listing.raw
    assert "Jane" not in json.dumps(listing.raw)
    assert "jane.example" not in json.dumps(listing.raw)


def test_a_house_available_later() -> None:
    listing = parse_item(items()[1], today=TODAY)

    assert listing.property_type is PropertyType.SINGLE_FAMILY
    assert listing.available_from == date(2026, 10, 13)
    assert (listing.beds, listing.baths) == (4, 2.0)


def test_fees_and_a_longer_minimum_stay() -> None:
    listing = parse_item(items()[2], today=TODAY)

    assert (listing.raw["deposit"], listing.raw["petDeposit"]) == ("$4200.00", "$300.00")
    assert listing.baths == 2.5
    assert listing.min_lease_months == 3


def test_a_listing_without_a_neighbourhood_overview_or_fees() -> None:
    listing = parse_item(items()[3], today=TODAY)

    assert listing.description == "Three-bedroom ranch home with a remodeled kitchen."
    assert {"deposit", "cleaningFee", "petDeposit", "applicationFee"}.isdisjoint(listing.raw)


def test_a_room() -> None:
    listing = parse_item(items()[4], today=TODAY)

    assert listing.property_type is PropertyType.ROOM
    assert listing.price == Price(amount=usd(1300))


def test_an_item_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no id"):
        parse_item(items()[5], today=TODAY)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"availableOn": "12/01/2026"}, date(2026, 12, 1)),
        ({"availableOnDate": "Available: Dec. 1, 2026"}, date(2026, 12, 1)),
        ({"availableOnDate": "Available on: December 1, 2026"}, date(2026, 12, 1)),
        ({"availableOn": "soon", "availableOnDate": "Available Dec 1, 2026"}, date(2026, 12, 1)),
        ({"isAvailableNow": True, "availableOn": "12/01/2026"}, TODAY),
        ({"availableOnDate": "Available"}, None),
        ({}, None),
    ],
)
def test_available_dates(item: dict[str, Any], expected: date | None) -> None:
    listing = parse_item({"listingId": "1_1", "name": "Place", **item}, today=TODAY)

    assert listing.available_from == expected


@pytest.mark.parametrize(
    ("item", "price"),
    [
        ({"monthlyRent": "1800.50"}, Price(amount=Money.from_major(Decimal("1800.50"), "USD"))),
        (
            {"monthlyRent": "0.00", "rentAmount": {"amount": "$1,200/month", "currency": "USD"}},
            Price(amount=usd(1200)),
        ),
        ({"rentAmount": {"amount": "$1,200/month"}}, Price(amount=usd(1200))),
        ({"monthlyRent": True}, None),
        ({"monthlyRent": "n/a"}, None),
        ({}, None),
    ],
)
def test_prices(item: dict[str, Any], price: Price | None) -> None:
    assert parse_item({"listingId": "1_1", "name": "Place", **item}, today=TODAY).price == price


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"propertyType": "Apartment"}, PropertyType.APARTMENT),
        ({"propertyType": "Entire Unit - Townhouse"}, PropertyType.TOWNHOUSE),
        ({"propertyType": "Hotel"}, PropertyType.OTHER),
        ({"propertyTypeClass": "room", "propertyType": "Room - House"}, PropertyType.ROOM),
        ({}, PropertyType.OTHER),
    ],
)
def test_property_types(item: dict[str, Any], expected: PropertyType) -> None:
    listing = parse_item({"listingId": "1_1", "name": "Place", **item}, today=TODAY)

    assert listing.property_type is expected


def test_odd_values_are_ignored_not_fatal() -> None:
    item = {
        "id": 7,
        "title": "Place",
        "bedroomCount": -1,
        "bathroomCount": "lots",
        "latitude": 40.0,
        "longitude": None,
        "url": "https://elsewhere.example.com/7",
        "photos": [{"url": "/relative.jpg"}, "https://img.example.com/a.jpg", None],
        "houseRulesGraphql": [None, {"name": "maxVehicles"}],
        "laundryType": "NONE",
    }

    listing = parse_item(item, today=TODAY)

    assert (listing.source_id, listing.title) == ("7", "Place")
    assert str(listing.url) == "https://www.furnishedfinder.com/property/7"
    assert (listing.beds, listing.baths, listing.lat, listing.lng) == (None, None, None, None)
    assert [str(url) for url in listing.images] == ["https://img.example.com/a.jpg"]
    assert listing.amenities == frozenset()


# --- build_input ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "url"),
    [
        ("Denver, CO", "https://www.furnishedfinder.com/housing/us--co--denver"),
        ("Long Beach, ca", "https://www.furnishedfinder.com/housing/us--ca--long-beach"),
        ("St. Louis, MO", "https://www.furnishedfinder.com/housing/us--mo--st-louis"),
        ("us--tx--austin", "https://www.furnishedfinder.com/housing/us--tx--austin"),
        ("Denver, Colorado", None),
        ("Denver", None),
        (", CO", None),
    ],
)
def test_search_urls(location: str, url: str | None) -> None:
    assert search_url(location) == url


def test_input_carries_the_price_range_in_each_url() -> None:
    criteria = Criteria(
        price_min=Money.from_major(Decimal("1200.50"), "USD"),
        price_max=Money.from_major(Decimal("2999.10"), "USD"),
        min_beds=2,
        property_types_deny=(PropertyType.ROOM,),
    )

    run_input = build_input(criteria, locations=("Denver, CO", "Nowhere"), max_results=50)

    assert run_input == {
        "startUrls": [
            {
                "url": "https://www.furnishedfinder.com/housing/us--co--denver?min-price=1200&max-price=3000"
            }
        ],
        "maxItems": 50,
        "moreDetails": True,
        "includeReviews": False,
        "enrichEmails": False,
    }


def test_input_without_a_price_range_has_bare_urls() -> None:
    run_input = build_input(Criteria(), locations=("Denver, CO",), max_results=10)

    assert run_input["startUrls"] == [
        {"url": "https://www.furnishedfinder.com/housing/us--co--denver"}
    ]


def test_non_usd_prices_are_not_sent() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    criteria = Criteria(
        price_min=Money.from_major(1000, "EUR"), price_max=Money.from_major(2000, "EUR")
    )

    run_input = build_input(criteria, locations=("Denver, CO",), max_results=10)

    assert "?" not in run_input["startUrls"][0]["url"]


# --- The adapter ---------------------------------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401
    entry = {"api_key": SecretStr(TOKEN), "search_locations": ("Denver, CO",), **overrides}
    return SourcesSettings(
        requests_per_minute=600, adapters={"furnished_finder": AdapterOverrides(**entry)}
    )


class Apify:
    """Fake Apify: the run succeeds at once and its dataset holds ``dataset``."""

    def __init__(self, dataset: list[Any]) -> None:
        self.dataset = dataset
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/runs"):
            return httpx2.Response(201, json=fixture("apify_run_succeeded.json"))
        if path.endswith("/items"):
            return httpx2.Response(200, json=self.dataset)
        return httpx2.Response(404)


def adapter(apify: Apify, source_settings: SourcesSettings | None = None) -> FurnishedFinder:
    config = resolve_config(FurnishedFinder, source_settings or settings())
    return FurnishedFinder(
        config, transport=httpx2.MockTransport(apify), sleep=lambda _: None, today=lambda: TODAY
    )


def test_search_returns_parsed_listings_and_skips_bad_items() -> None:
    apify = Apify(items())

    listings = adapter(apify).search(Criteria(price_max=usd(5000)))

    assert [listing.source_id for listing in listings] == [
        "10000001_1",
        "10000002_1",
        "10000003_1",
        "10000004_1",
        "10000005_1",
    ]
    assert all(isinstance(listing, NormalizedListing) for listing in listings)
    start = apify.requests[0]
    assert str(start.url).startswith(
        f"{APIFY_API}/acts/memo23~furnishedfinder-scraper-cheerio/runs"
    )
    body = json.loads(start.content)
    assert (body["startUrls"], body["maxItems"]) == (
        [{"url": "https://www.furnishedfinder.com/housing/us--co--denver?max-price=5000"}],
        25,
    )
    # The actor gets the run timeout less a margin, so Apify stops first.
    assert start.url.params["timeout"] == "840"


def test_an_empty_dataset_is_no_listings() -> None:
    assert adapter(Apify(fixture("dataset_empty.json"))).search(Criteria()) == []


def test_robots_txt_is_not_fetched_for_the_api() -> None:
    apify = Apify(items())

    adapter(apify, settings(respect_robots=True)).search(Criteria())

    assert not any(request.url.path == "/robots.txt" for request in apify.requests)


def test_no_search_locations_fails_with_a_hint() -> None:
    apify = Apify([])

    with pytest.raises(SourceError, match="no search locations"):
        adapter(apify, settings(search_locations=None)).search(Criteria())

    assert apify.requests == []


def test_locations_without_a_state_fail_before_anything_is_billed() -> None:
    apify = Apify([])

    with pytest.raises(SourceError, match="two-letter state"):
        adapter(apify, settings(search_locations=("Denver", "Boulder, Colorado"))).search(
            Criteria()
        )

    assert apify.requests == []


def test_no_api_key_fails_with_a_hint() -> None:
    config = resolve_config(FurnishedFinder, settings(api_key=None))
    apify = Apify([])

    with pytest.raises(SourceError, match="no Apify API token"):
        FurnishedFinder(config, transport=httpx2.MockTransport(apify)).search(Criteria())


def test_discovered_and_runs_when_switched_on() -> None:
    registry = discover()
    assert registry.get("furnished_finder") is FurnishedFinder

    by_name = {source.config.name: source for source in registry.configured(settings(enabled=True))}
    config = by_name["furnished_finder"].config
    assert config.blocker is None
    assert config.enabled is True
    assert config.disabled_reason is None


# --- Owner type ----------------------------------------------------------------------


def test_a_management_company_is_a_property_manager() -> None:
    assert parse_item(items()[0], today=TODAY).owner_type is OwnerType.PROPERTY_MANAGER


@pytest.mark.parametrize(
    ("company", "description", "expected"),
    [
        # Landlords also fill in an employer, so a plain name is no hint.
        ("Example High School", "A bright furnished room.", OwnerType.UNKNOWN),
        ("Example", "A bright furnished room.", OwnerType.UNKNOWN),
        (None, "I am the owner and live nearby.", OwnerType.INDIVIDUAL),
        (None, "Managed by Example Property Management.", OwnerType.PROPERTY_MANAGER),
        ("Example Stays LLC", "I am the owner and live nearby.", OwnerType.PROPERTY_MANAGER),
    ],
)
def test_owner_type(company: str | None, description: str, expected: OwnerType) -> None:
    item = {
        "listingId": "1_1",
        "name": "Room",
        "description": description,
        "landlordCompany": company,
    }

    assert parse_item(item, today=TODAY).owner_type is expected
