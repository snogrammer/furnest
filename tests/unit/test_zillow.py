"""The Zillow adapter: criteria → actor input, actor items → listings, and a whole
search against a faked Apify. Runs offline on tests/fixtures/zillow."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Amenity, Criteria, Money, Price, PropertyType
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.zillow import Zillow
from furnest.sources.adapters.zillow.parse import build_input, parse_item
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zillow"
TODAY = date(2026, 10, 7)
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    return fixture("dataset_items.json")  # type: ignore[no-any-return]


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


def one(item: dict[str, Any]) -> Any:  # noqa: ANN401 - a NormalizedListing
    (listing,) = parse_item(item, today=TODAY)
    return listing


# --- parse_item: single homes ---------------------------------------------------------


def test_a_house_with_details_maps_every_documented_field() -> None:
    listing = one(items()[0])

    assert listing.source == "zillow"
    assert listing.source_id == "900000001"
    assert str(listing.url) == "https://www.zillow.com/homedetails/900000001_zpid/"
    assert listing.title == "100 Example St"
    assert listing.description == (
        "Fully furnished two-bedroom house with a fenced yard. Pets welcome."
    )
    assert listing.price == Price(amount=usd(2400))
    address = listing.address
    assert (address.line1, address.city, address.region, address.postal_code) == (
        "100 Example St",
        "Exampleville",
        "XX",
        "00001",
    )
    assert address.country == "US"
    assert (listing.lat, listing.lng) == (40.0, -100.0)
    assert (listing.beds, listing.baths, listing.sqft) == (2, 1.5, 1150)
    assert listing.property_type is PropertyType.SINGLE_FAMILY
    assert listing.furnished is True
    assert listing.available_from == date(2026, 11, 1)
    assert listing.min_lease_months == 6
    assert [str(url) for url in listing.images] == [
        "https://photos.zillowstatic.com/fp/00000000000000000000000000000001-p_f.jpg",
        "https://photos.zillowstatic.com/fp/00000000000000000000000000000002-p_f.jpg",
    ]
    assert listing.contact.platform_only is True
    assert (listing.contact.name, listing.contact.email, listing.contact.phone) == (
        None,
        None,
        None,
    )


def test_fact_lists_become_words_the_extractor_reads() -> None:
    listing = one(items()[0])

    assert listing.amenities == {
        "dishwasher",
        "refrigerator",
        "central air",
        "in unit laundry",
        "garage",
    }
    found = KeywordAmenityExtractor().extract(listing)
    for tag in (Amenity.AC, Amenity.DISHWASHER, Amenity.IN_UNIT_LAUNDRY, Amenity.PETS_ALLOWED):
        assert tag in found.present, tag


def test_agents_and_bulky_details_are_dropped_from_raw() -> None:
    listing = one(items()[0])

    assert "Jane Example" not in json.dumps(listing.raw)
    assert "555-0100" not in json.dumps(listing.raw)
    assert "propertyDisplayRules" not in listing.raw
    assert set(listing.raw["_details"]) == {"description", "resoFacts"}
    assert listing.raw["daysOnZillow"] == 3


def test_an_apartment_without_details_and_with_unknowns() -> None:
    listing = one(items()[1])

    assert listing.source_id == "900000002"
    # No URL in the item: built from the zpid.
    assert str(listing.url) == "https://www.zillow.com/homedetails/900000002_zpid/"
    assert listing.price is None  # 0 is not a rent
    assert listing.property_type is PropertyType.STUDIO  # an apartment with 0 beds
    assert (listing.beds, listing.sqft) == (0, None)
    assert (listing.lat, listing.lng) == (None, None)  # both or neither
    assert listing.description == ""
    assert listing.amenities == frozenset()
    assert (listing.available_from, listing.min_lease_months) == (None, None)
    assert [str(url) for url in listing.images] == [
        "https://photos.zillowstatic.com/fp/00000000000000000000000000000003-p_f.jpg"
    ]


def test_an_item_without_a_zpid_is_rejected() -> None:
    with pytest.raises(ValueError, match="no zpid"):
        parse_item(items()[3], today=TODAY)


# --- parse_item: apartment buildings -------------------------------------------------


def test_a_building_becomes_one_listing_per_bedroom_group() -> None:
    studio, one_bed = parse_item(items()[2], today=TODAY)

    assert (studio.source_id, one_bed.source_id) == ("500000001-0bd", "500000001-1bd")
    assert {str(listing.url) for listing in (studio, one_bed)} == {
        "https://www.zillow.com/apartments/exampleville-xx/the-example-lofts/500000001/"
    }
    assert (studio.beds, studio.price) == (0, Price(amount=usd(1650)))
    assert (one_bed.beds, one_bed.price) == (1, Price(amount=usd(1900)))
    assert (studio.property_type, one_bed.property_type) == (
        PropertyType.STUDIO,
        PropertyType.APARTMENT,
    )
    assert studio.title == "The Example Lofts"
    assert (one_bed.address.line1, one_bed.lat, one_bed.lng) == (
        "300 Placeholder Blvd",
        40.2,
        -100.2,
    )
    assert one_bed.furnished is True


@pytest.mark.parametrize(
    ("item", "error"),
    [
        ({"_type": "propertyGroup", "url": "https://www.zillow.com/b/1/"}, "no zpid or lotId"),
        ({"_type": "propertyGroup", "lotId": 1, "unitsGroup": []}, "has no URL"),
        (
            {"_type": "propertyGroup", "lotId": 1, "hdpUrl": "/b/1/", "unitsGroup": [7, {}]},
            "no unit groups",
        ),
    ],
)
def test_unusable_buildings_are_rejected(item: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_item(item, today=TODAY)


def test_a_relative_building_url_is_made_absolute() -> None:
    item = {"_type": "propertyGroup", "zpid": 9, "hdpUrl": "/b/9/", "unitsGroup": [{"beds": 2}]}

    (listing,) = parse_item(item, today=TODAY)

    assert (listing.source_id, str(listing.url)) == ("9-2bd", "https://www.zillow.com/b/9/")
    assert (listing.price, listing.title) == (None, "2 bd")


def test_a_unit_group_for_rooms_is_a_room() -> None:
    building = {
        "_type": "propertyGroup",
        "zpid": 7,
        "url": "https://www.zillow.com/b/7/",
        "unitsGroup": [{"bedrooms": 1, "minPrice": 900, "isRoomForRent": True}],
    }

    (listing,) = parse_item(building, today=TODAY)

    assert listing.property_type is PropertyType.ROOM


# --- parse_item: a live run ----------------------------------------------------------


def test_a_live_run_maps_as_recorded() -> None:
    listings = [
        listing
        for item in fixture("dataset_live.json")
        for listing in parse_item(item, today=TODAY)
    ]

    summary = [
        (
            listing.source_id,
            listing.title,
            listing.price.amount if listing.price else None,
            listing.beds,
            listing.property_type,
            listing.available_from,
            listing.min_lease_months,
        )
        for listing in listings
    ]
    apartment, condo = PropertyType.APARTMENT, PropertyType.CONDO
    oct13 = date(2026, 10, 13)
    assert summary == [
        ("100000001", "123 Example St", usd(9021), 4, apartment, date(2027, 1, 2), 1),
        ("100000002-1bd", "Example Heights", usd(935), 1, apartment, None, None),
        ("100000002-2bd", "Example Heights", usd(1025), 2, apartment, None, None),
        ("100000002-3bd", "Example Heights", usd(1325), 3, apartment, None, None),
        ("100000003-1bd", "Example Point Apartments", usd(811), 1, apartment, oct13, 12),
        ("100000003-2bd", "Example Point Apartments", usd(1061), 2, apartment, oct13, 12),
        ("100000004", "123 Example St", usd(3250), 1, condo, date(2026, 10, 21), 12),
    ]
    house = listings[0]
    assert (house.baths, house.sqft, house.address.city, house.address.region) == (
        3.0,
        2669,
        "Exampleville",
        "TX",
    )
    assert len(house.images) == 3
    assert {"dishwasher", "fitness center"} <= house.amenities
    assert all(listing.contact.platform_only for listing in listings)
    assert not any("attributionInfo" in listing.raw for listing in listings)


# --- parse_item: field details -------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ({"value": "2,100"}, Price(amount=usd(2100))),
        ({"value": 1999.5}, Price(amount=Money.from_major(Decimal("1999.5"), "USD"))),
        ({"value": True}, None),
        ({"value": float("inf")}, None),
        ("$1,500/mo", None),  # not the documented shape
        (None, None),
    ],
)
def test_prices(price: object, expected: Price | None) -> None:
    assert one({"zpid": 1, "price": price}).price == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"propertyType": "condo"}, PropertyType.CONDO),
        ({"propertyType": "townhome"}, PropertyType.TOWNHOUSE),
        ({"homeType": "SINGLE_FAMILY"}, PropertyType.SINGLE_FAMILY),
        ({"propertyType": "multiFamily", "homeType": "APARTMENT"}, PropertyType.APARTMENT),
        ({"isRoomForRent": True, "propertyType": "singleFamily"}, PropertyType.ROOM),
        ({"rental": {"isRoomForRent": True}, "propertyType": "apartment"}, PropertyType.ROOM),
        ({"rental": {"isRoomForRent": False}, "propertyType": "apartment"}, PropertyType.APARTMENT),
        ({"propertyType": "lot"}, PropertyType.OTHER),
        ({}, PropertyType.OTHER),
    ],
)
def test_property_types(item: dict[str, Any], expected: PropertyType) -> None:
    assert one({"zpid": 1, **item}).property_type is expected


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"availabilityDate": "2026-12-01T00:00:00"}, date(2026, 12, 1)),
        ({"resoFacts": {"availabilityDate": "12/01/2026"}}, date(2026, 12, 1)),
        ({"availabilityDate": "Dec 1, 2026"}, date(2026, 12, 1)),
        ({"availabilityDate": "Now"}, TODAY),
        ({"availabilityDate": "Soon"}, None),
        # resoFacts as live runs send it: epoch milliseconds, midnight UTC.
        ({"resoFacts": {"availabilityDate": 1798848000000}}, date(2027, 1, 2)),
        ({"resoFacts": {"availabilityDate": 10**20}}, None),
    ],
)
def test_available_dates(details: dict[str, Any], expected: date | None) -> None:
    assert one({"zpid": 1, "_details": details}).available_from == expected


def test_a_top_level_availability_date_is_read_without_details() -> None:
    assert one({"zpid": 1, "availabilityDate": "2026-11-15"}).available_from == date(2026, 11, 15)


@pytest.mark.parametrize(
    ("term", "months"),
    [("12 months", 12), ("3-month minimum", 3), ("0 mo", 1), ("Short term", None), (None, None)],
)
def test_lease_terms(term: str | None, months: int | None) -> None:
    item = {"zpid": 1, "_details": {"resoFacts": {"leaseTerm": term}}}

    assert one(item).min_lease_months == months


def test_laundry_that_names_itself_is_kept_as_is() -> None:
    item = {"zpid": 1, "_details": {"resoFacts": {"laundryFeatures": ["Washer Hookup", " "]}}}

    assert one(item).amenities == {"washer hookup"}


def test_odd_values_are_ignored_not_fatal() -> None:
    item = {
        "zpid": 7,
        "url": "https://elsewhere.example.com/7",
        "bedrooms": -1,
        "bathrooms": "lots",
        "location": "40,-100",
        "media": {"allPropertyPhotos": {"highResolution": ["not-a-url", 3]}},
        "_details": "nope",
    }

    listing = one(item)

    assert str(listing.url) == "https://www.zillow.com/homedetails/7_zpid/"
    assert (listing.beds, listing.baths, listing.lat, listing.lng) == (None, None, None, None)
    assert listing.images == ()
    assert listing.title == "Rental"
    assert "_details" not in listing.raw


def test_a_title_falls_back_to_size_and_city() -> None:
    item = {"zpid": 1, "bedrooms": 3, "address": {"city": "Exampleville"}}

    assert one(item).title == "3 bd in Exampleville"


# --- build_input ---------------------------------------------------------------------


def test_input_always_asks_for_furnished_rentals_and_carries_supported_filters() -> None:
    criteria = Criteria(
        price_min=usd(1000),
        price_max=usd(3000),
        min_beds=2,
        min_baths=1.5,
        property_types_deny=(PropertyType.ROOM,),
        move_in_latest=date(2026, 12, 1),
    )

    run_input = build_input(criteria, locations=("Exampleville, XX",), max_results=50, today=TODAY)

    assert run_input == {
        "operation": "rent",
        "locations": ["Exampleville, XX"],
        "furnished": True,
        "space": "entirePlace",
        "sortBy": "newest",
        "fetchDetails": True,
        "maxItems": 50,
        "minBeds": 2,
        "minBaths": 1,
        "moveInDate": "2026-12-01",
    }


def test_input_leaves_out_what_criteria_leave_open() -> None:
    criteria = Criteria(min_beds=0, min_baths=0.5, move_in_latest=date(2026, 1, 1))

    run_input = build_input(criteria, locations=("A", "B"), max_results=10, today=TODAY)

    assert run_input["space"] == ""
    # Price is never sent: the actor's rent price unit is ambiguous. A past move-in
    # date would filter out everything.
    assert {"minPrice", "maxPrice", "minBeds", "minBaths", "moveInDate"}.isdisjoint(run_input)


@pytest.mark.parametrize(
    ("allow", "deny", "expected"),
    [
        ((PropertyType.ROOM,), (), "roomForRent"),
        ((PropertyType.APARTMENT, PropertyType.STUDIO), (), "entirePlace"),
        ((PropertyType.APARTMENT, PropertyType.ROOM), (), ""),
        ((), (PropertyType.ROOM,), "entirePlace"),
        ((), (PropertyType.CONDO,), ""),
    ],
)
def test_space(
    allow: tuple[PropertyType, ...], deny: tuple[PropertyType, ...], expected: str
) -> None:
    criteria = Criteria(property_types_allow=allow, property_types_deny=deny)

    assert build_input(criteria, locations=("A",), max_results=10, today=TODAY)["space"] == expected


# --- The adapter ---------------------------------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401
    entry = {"api_key": SecretStr(TOKEN), "search_locations": ("Exampleville, XX",), **overrides}
    return SourcesSettings(requests_per_minute=600, adapters={"zillow": AdapterOverrides(**entry)})


class Apify:
    """Fake Apify: the run ends at once as ``run`` says, and its dataset holds ``dataset``."""

    def __init__(self, dataset: list[Any], run: str = "apify_run_succeeded.json") -> None:
        self.dataset = dataset
        self.run = run
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/runs"):
            return httpx2.Response(201, json=fixture(self.run))
        if path.endswith("/items"):
            return httpx2.Response(200, json=self.dataset)
        return httpx2.Response(404)


def adapter(apify: Apify, source_settings: SourcesSettings | None = None) -> Zillow:
    config = resolve_config(Zillow, source_settings or settings())
    return Zillow(
        config, transport=httpx2.MockTransport(apify), sleep=lambda _: None, today=lambda: TODAY
    )


def test_search_returns_parsed_listings_and_skips_bad_items() -> None:
    apify = Apify(items())

    listings = adapter(apify).search(Criteria(min_beds=1))

    assert [listing.source_id for listing in listings] == [
        "900000001",
        "900000002",
        "500000001-0bd",
        "500000001-1bd",
    ]
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/igolaizola~zillow-scraper-ppe/runs")
    body = json.loads(start.content)
    assert (body["locations"], body["furnished"], body["minBeds"], body["maxItems"]) == (
        ["Exampleville, XX"],
        True,
        1,
        100,
    )
    assert start.url.params["timeout"] == "840"
    assert not any(request.url.path == "/robots.txt" for request in apify.requests)


def test_an_empty_dataset_is_no_listings() -> None:
    assert adapter(Apify(fixture("dataset_empty.json"))).search(Criteria()) == []


def test_a_failed_run_fails_the_search() -> None:
    apify = Apify(items(), run="apify_run_failed.json")

    with pytest.raises(SourceError, match="ended FAILED"):
        adapter(apify).search(Criteria())


def test_no_search_locations_fails_with_a_hint() -> None:
    apify = Apify([])

    with pytest.raises(SourceError, match='set "search_locations"'):
        adapter(apify, settings(search_locations=None)).search(Criteria())

    assert apify.requests == []


def test_no_api_key_fails_with_a_hint() -> None:
    config = resolve_config(Zillow, settings(api_key=None))

    with pytest.raises(SourceError, match='set "api_key"'):
        Zillow(config, transport=httpx2.MockTransport(Apify([]))).search(Criteria())


def test_discovered_and_disabled_until_it_has_a_key() -> None:
    registry = discover()
    assert registry.get("zillow") is Zillow

    by_name = {source.config.name: source for source in registry.configured(SourcesSettings())}
    assert by_name["zillow"].config.enabled is False
    assert by_name["zillow"].config.disabled_reason == (
        'needs an API key: set "api_key" for zillow in SOURCES_ADAPTERS'
    )

    keyed = {source.config.name: source for source in registry.configured(settings(enabled=True))}
    assert (keyed["zillow"].config.enabled, keyed["zillow"].config.disabled_reason) == (True, None)
