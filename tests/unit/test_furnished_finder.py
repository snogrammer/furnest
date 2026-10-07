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
    Price,
    PropertyType,
    register_currency,
)
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.furnished_finder import FurnishedFinder
from furnest.sources.adapters.furnished_finder.parse import build_input, parse_item
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "furnished_finder"
TODAY = date(2026, 10, 5)
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    return fixture("dataset_items.json")  # type: ignore[no-any-return]


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


# --- parse_item ----------------------------------------------------------------------


def test_an_entire_unit_maps_every_documented_field() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert listing.source == "furnished_finder"
    assert listing.source_id == "10000001_1"
    assert str(listing.url) == "https://www.furnishedfinder.com/property/10000001_1"
    assert listing.title == "Bright Two-Bedroom House Near the Medical Center"
    assert listing.description == (
        "Quiet two-bedroom house with a king bed and a fenced yard.\n\n"
        "Entire house; the garage is not included.\n\n"
        "Walkable street with a grocery store nearby."
    )
    assert listing.price == Price(amount=usd(2450))
    assert (listing.address.city, listing.address.region, listing.address.country) == (
        "Exampleville",
        "XX",
        "US",
    )
    assert (listing.lat, listing.lng) == (40.0, -100.0)
    assert (listing.beds, listing.baths, listing.sqft) == (2, 1.5, 1100)
    assert listing.property_type is PropertyType.SINGLE_FAMILY
    assert listing.furnished is True
    assert listing.available_from == TODAY
    assert listing.min_lease_months == 1
    assert [str(url) for url in listing.images] == [
        "https://staticproperties.furnishedfinder.com/10000001/1/10000001_1_00000001-full.jpg",
        "https://staticproperties.furnishedfinder.com/10000001/1/10000001_1_00000002-full.jpg",
    ]
    assert listing.contact.platform_only is True
    assert (listing.contact.name, listing.contact.email, listing.contact.phone) == (
        None,
        None,
        None,
    )


def test_amenity_keys_become_words_the_extractor_reads() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert listing.amenities == {
        "air conditioning",
        "dishwasher",
        "wifi",
        "parking",
        "washer and dryer",
        "non smoking",
        "in-unit w/d",
        "utilities included",
    }
    found = KeywordAmenityExtractor().extract(listing)
    for tag in (
        Amenity.AC,
        Amenity.DISHWASHER,
        Amenity.WIFI,
        Amenity.IN_UNIT_LAUNDRY,
        Amenity.SMOKE_FREE,
        Amenity.UTILITIES_INCLUDED,
        Amenity.KING_BED,
    ):
        assert tag in found.present, tag
    # "petsLiveOnThisProperty" is the landlord's pets, so it must not read as pets allowed.
    assert Amenity.PETS_ALLOWED not in found.present


def test_reviews_are_dropped_from_raw() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert "reviews" not in listing.raw
    assert "Jane Example" not in json.dumps(listing.raw)
    assert listing.raw["houseRules"] == [
        {"name": "No smoking", "description": "Smoking is not allowed."}
    ]


def test_a_room_with_a_text_price_and_a_future_date() -> None:
    listing = parse_item(items()[1], today=TODAY)

    assert listing.source_id == "10000002_3"
    assert str(listing.url) == "https://www.furnishedfinder.com/property/10000002_3"
    assert listing.price == Price(amount=usd(950))
    assert listing.property_type is PropertyType.ROOM
    assert (listing.lat, listing.lng) == (None, None)
    assert listing.available_from == date(2026, 11, 15)
    assert listing.min_lease_months == 3
    assert listing.images == ()
    assert listing.amenities == {"wifi", "utilities included"}


def test_a_studio_with_unknowns() -> None:
    listing = parse_item(items()[2], today=TODAY)

    assert listing.beds == 0
    assert listing.property_type is PropertyType.STUDIO
    assert listing.sqft is None  # 0 means not given
    assert listing.available_from is None  # "Available" without a date
    assert listing.min_lease_months is None
    assert listing.description == ""


def test_an_item_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no id"):
        parse_item(items()[3], today=TODAY)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-12-01", date(2026, 12, 1)),
        ("12/01/2026", date(2026, 12, 1)),
        ("Available Dec 1, 2026", date(2026, 12, 1)),
        ("Available on: December 1, 2026", date(2026, 12, 1)),
        ("Soon", None),
        ("", None),
    ],
)
def test_available_dates(text: str, expected: date | None) -> None:
    item = {"id": "1_1", "name": "Place", "availableOnDate": text}

    assert parse_item(item, today=TODAY).available_from == expected


@pytest.mark.parametrize(
    ("item", "price"),
    [
        ({"priceValue": "1800.50"}, Price(amount=Money.from_major(Decimal("1800.50"), "USD"))),
        ({"priceValue": 0, "priceFormatted": "$1,200/month"}, Price(amount=usd(1200))),
        ({"priceValue": True}, None),
        ({"priceValue": "n/a"}, None),
        ({}, None),
    ],
)
def test_prices(item: dict[str, Any], price: Price | None) -> None:
    assert parse_item({"id": "1_1", "name": "Place", **item}, today=TODAY).price == price


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"propertyType": "Apartment"}, PropertyType.APARTMENT),
        ({"propertyType": "Entire Unit - Townhouse"}, PropertyType.TOWNHOUSE),
        ({"propertyType": "Hotel"}, PropertyType.OTHER),
        ({"propertyTypeClass": "room", "propertyType": "Apartment"}, PropertyType.ROOM),
        ({}, PropertyType.OTHER),
    ],
)
def test_property_types(item: dict[str, Any], expected: PropertyType) -> None:
    assert parse_item({"id": "1_1", "name": "Place", **item}, today=TODAY).property_type is expected


def test_odd_numbers_are_ignored_not_fatal() -> None:
    item = {
        "id": 7,
        "name": "Place",
        "bedroomCount": -1,
        "bathroomCount": "lots",
        "latitude": 40.0,
        "longitude": None,
        "url": "https://elsewhere.example.com/7",
    }

    listing = parse_item(item, today=TODAY)

    assert listing.source_id == "7"
    assert str(listing.url) == "https://www.furnishedfinder.com/property/7"
    assert (listing.beds, listing.baths, listing.lat, listing.lng) == (None, None, None, None)


# --- build_input ---------------------------------------------------------------------


def test_input_carries_the_filters_the_actor_supports() -> None:
    criteria = Criteria(
        price_min=Money.from_major(Decimal("1200.50"), "USD"),
        price_max=Money.from_major(Decimal("2999.10"), "USD"),
        min_beds=2,
        min_baths=1.5,
        property_types_deny=(PropertyType.ROOM,),
    )

    run_input = build_input(criteria, locations=("Exampleville, XX",), max_results=50)

    assert run_input == {
        "mode": "search",
        "locations": ["Exampleville, XX"],
        "propertyTypeClass": "entire_unit",
        "fetchDetails": True,
        "fetchReviews": False,
        "maxPages": 0,
        "maxListings": 50,
        "minPrice": 1200,
        "maxPrice": 3000,
        "minBedrooms": 2,
        "minBathrooms": 1,
    }


def test_input_leaves_out_what_criteria_leave_open() -> None:
    run_input = build_input(Criteria(), locations=("A", "B"), max_results=10)

    assert run_input["propertyTypeClass"] == "any"
    assert {"minPrice", "maxPrice", "minBedrooms", "minBathrooms"}.isdisjoint(run_input)


def test_non_usd_prices_are_not_sent() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    criteria = Criteria(
        price_min=Money.from_major(1000, "EUR"), price_max=Money.from_major(2000, "EUR")
    )

    assert "minPrice" not in build_input(criteria, locations=("A",), max_results=10)


@pytest.mark.parametrize(
    ("allow", "deny", "expected"),
    [
        ((PropertyType.ROOM,), (), "room"),
        ((PropertyType.APARTMENT, PropertyType.STUDIO), (), "entire_unit"),
        ((PropertyType.APARTMENT, PropertyType.ROOM), (), "any"),
        ((), (PropertyType.ROOM,), "entire_unit"),
        ((), (PropertyType.CONDO,), "any"),
    ],
)
def test_property_type_class(
    allow: tuple[PropertyType, ...], deny: tuple[PropertyType, ...], expected: str
) -> None:
    criteria = Criteria(property_types_allow=allow, property_types_deny=deny)

    assert build_input(criteria, locations=("A",), max_results=10)["propertyTypeClass"] == expected


# --- The adapter ---------------------------------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401
    entry = {"api_key": SecretStr(TOKEN), "search_locations": ("Exampleville, XX",), **overrides}
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

    listings = adapter(apify).search(Criteria(min_beds=1))

    assert [listing.source_id for listing in listings] == ["10000001_1", "10000002_3", "10000003_1"]
    assert all(isinstance(listing, NormalizedListing) for listing in listings)
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/abotapi~furnishedfinder-scraper/runs")
    body = json.loads(start.content)
    assert (body["locations"], body["minBedrooms"], body["maxListings"]) == (
        ["Exampleville, XX"],
        1,
        100,
    )
    # The actor gets the run timeout less a margin, so Apify stops first.
    assert start.url.params["timeout"] == "840"


def test_robots_txt_is_not_fetched_for_the_api() -> None:
    apify = Apify(items())

    adapter(apify, settings(respect_robots=True)).search(Criteria())

    assert not any(request.url.path == "/robots.txt" for request in apify.requests)


def test_the_providers_nothing_scanned_row_fails_the_run() -> None:
    apify = Apify(fixture("dataset_error_row.json") + items())

    with pytest.raises(SourceError, match="scanned nothing for Exampleville, XX"):
        adapter(apify).search(Criteria())


def test_no_search_locations_fails_with_a_hint() -> None:
    apify = Apify([])

    with pytest.raises(SourceError, match='set "search_locations"'):
        adapter(apify, settings(search_locations=None)).search(Criteria())

    assert apify.requests == []


def test_no_api_key_fails_with_a_hint() -> None:
    config = resolve_config(FurnishedFinder, settings(api_key=None))
    apify = Apify([])

    with pytest.raises(SourceError, match='set "api_key"'):
        FurnishedFinder(config, transport=httpx2.MockTransport(apify)).search(Criteria())


def test_discovered_and_disabled_until_it_has_a_key() -> None:
    registry = discover()
    assert registry.get("furnished_finder") is FurnishedFinder

    by_name = {source.config.name: source for source in registry.configured(SourcesSettings())}
    source = by_name["furnished_finder"]
    assert source.config.enabled is False
    assert source.config.disabled_reason == (
        'needs an API key: set "api_key" for furnished_finder in SOURCES_ADAPTERS'
    )

    keyed = {s.config.name: s for s in registry.configured(settings(enabled=True))}
    assert (
        keyed["furnished_finder"].config.enabled,
        keyed["furnished_finder"].config.disabled_reason,
    ) == (True, None)
