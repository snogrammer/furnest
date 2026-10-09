"""The Airbnb adapter: criteria → actor input, actor items → listings, and a whole
search against a faked Apify. Runs offline on tests/fixtures/airbnb."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Amenity, Criteria, Money, OwnerType, Price, PropertyType, RentPeriod
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.airbnb import Airbnb
from furnest.sources.adapters.airbnb.parse import (
    StayWindow,
    build_input,
    parse_item,
    stay_window,
    window_of,
)
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "airbnb"
TODAY = date(2026, 10, 7)
# The window a default search asks for: 31 nights, as the fixtures were priced.
WINDOW = StayWindow(date(2026, 10, 7), date(2026, 11, 7))
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    return fixture("dataset_items.json")  # type: ignore[no-any-return]


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


def one(item: dict[str, Any]) -> Any:  # noqa: ANN401 - a NormalizedListing
    return parse_item(item, window=WINDOW)


def minimal(**fields: Any) -> dict[str, Any]:  # noqa: ANN401 - item fields
    return {"id": "1", "title": "A stay", **fields}


# --- parse_item ---------------------------------------------------------------------


def test_an_entire_place_maps_every_documented_field() -> None:
    listing = one(items()[0])

    assert listing.source == "airbnb"
    assert listing.source_id == "700000001"
    assert str(listing.url) == "https://www.airbnb.com/rooms/700000001"
    assert listing.title == "Bright One-Bedroom Near the Park"
    assert listing.description.startswith("Entire rental unit in Exampleville")
    # $3,302 for 31 nights, all fees in, spread over a month: 3302 x (365/12) / 31.
    assert listing.price == Price(amount=Money(amount_minor=323987, currency="USD"))
    address = listing.address
    assert (address.line1, address.city, address.region, address.country) == (
        None,
        "Exampleville",
        "Illinois",
        "US",
    )
    assert (listing.lat, listing.lng) == (40.1, -89.6)
    assert (listing.beds, listing.baths, listing.sqft) == (1, 1.0, None)
    assert listing.property_type is PropertyType.APARTMENT  # "rental unit"
    assert listing.furnished is True
    assert listing.available_from == WINDOW.check_in
    assert [str(url) for url in listing.images] == [
        "https://a0.muscache.com/pictures/example-1a.jpeg",
        "https://a0.muscache.com/pictures/example-1b.jpeg",
    ]
    assert listing.contact.platform_only is True
    assert (listing.contact.name, listing.contact.email) == (None, None)


def test_amenities_become_words_the_extractor_reads() -> None:
    listing = one(items()[0])

    assert listing.amenities == {
        "washer",
        "dryer",
        "dishwasher",
        "wifi",
        "dedicated workspace",
        "free parking on premises",
        "no air conditioning",
    }
    found = KeywordAmenityExtractor().extract(listing)
    for tag in (Amenity.DISHWASHER, Amenity.WIFI, Amenity.FREE_PARKING):
        assert tag in found.present, tag
    assert Amenity.AC in found.absent


def test_hosts_and_app_links_are_dropped_from_raw() -> None:
    listing = one(items()[0])

    dumped = json.dumps(listing.raw)
    assert "Jane Example" not in dumped
    assert "John Example" not in dumped
    assert {"host", "coHosts", "androidLink", "iosLink"}.isdisjoint(listing.raw)
    assert listing.raw["personCapacity"] == 2


def test_a_private_room_quoted_per_night() -> None:
    listing = one(items()[1])

    assert listing.source_id == "700000002"  # a numeric id
    assert listing.title == "Quiet Room"
    assert listing.property_type is PropertyType.ROOM
    assert listing.price == Price(amount=usd(45), period=RentPeriod.NIGHTLY)
    assert listing.price.monthly == Money(amount_minor=136875, currency="USD")  # 45 x 365/12
    assert (listing.beds, listing.baths) == (1, 1.0)  # "1 shared bath"
    # "Private room in home in …": the place is after the last " in ".
    assert (listing.address.city, listing.address.region) == ("Exampleville", "Illinois")
    assert listing.amenities == frozenset()
    assert [str(url) for url in listing.images] == [
        "https://a0.muscache.com/pictures/example-thumb-2.jpeg"
    ]


def test_a_studio_outside_the_us_with_odd_values() -> None:
    listing = one(items()[2])

    # No title: the sub-description stands in. A foreign URL: built from the id.
    assert listing.title == "Entire condo in Exampletown, Ontario, Canada"
    assert str(listing.url) == "https://www.airbnb.com/rooms/700000003"
    assert listing.price == Price(amount=usd(2100))
    assert (listing.beds, listing.baths) == (0, 1.5)
    assert listing.property_type is PropertyType.STUDIO  # a condo with 0 bedrooms
    # Not a US state, so only the city is kept.
    assert (listing.address.city, listing.address.region) == ("Exampletown", None)
    assert (listing.lat, listing.lng) == (None, None)  # both or neither
    assert listing.images == ()


def test_an_item_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no id"):
        parse_item(items()[3], window=WINDOW)


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        # The stay's total wins over the quoted rate.
        (
            {
                "amount": "$95",
                "qualifier": "night",
                "breakDown": {"totalBeforeTaxes": {"price": "$3,100"}},
            },
            Price(amount=Money(amount_minor=304167, currency="USD")),
        ),
        ({"amount": "$95", "qualifier": "night"}, Price(amount=usd(95), period=RentPeriod.NIGHTLY)),
        ({"amount": "$600", "qualifier": "week"}, Price(amount=usd(600), period=RentPeriod.WEEKLY)),
        ({"amount": "$2,000", "qualifier": "month"}, Price(amount=usd(2000))),
        # No qualifier: Airbnb quotes per night.
        ({"amount": "$80"}, Price(amount=usd(80), period=RentPeriod.NIGHTLY)),
        ({"amount": "", "qualifier": "night"}, None),
        ({"breakDown": {"totalBeforeTaxes": {"price": "free"}}}, None),
        ({}, None),
    ],
)
def test_prices(price: dict[str, Any], expected: Price | None) -> None:
    assert one(minimal(price=price)).price == expected


def test_a_window_without_nights_falls_back_to_the_quoted_rate() -> None:
    item = minimal(
        price={
            "amount": "$90",
            "qualifier": "night",
            "breakDown": {"totalBeforeTaxes": {"price": "$900"}},
        }
    )
    same_day = StayWindow(TODAY, TODAY)

    assert parse_item(item, window=same_day).price == Price(
        amount=usd(90), period=RentPeriod.NIGHTLY
    )


@pytest.mark.parametrize(
    ("room_type", "sub_title", "sizes", "expected"),
    [
        ("Shared room", "Shared room in hostel in A, B, United States", [], PropertyType.ROOM),
        ("Entire home/apt", "Entire home in A, B, United States", [], PropertyType.SINGLE_FAMILY),
        ("Entire home/apt", "Entire townhouse in A", ["2 bedrooms"], PropertyType.TOWNHOUSE),
        ("Entire home/apt", "Entire serviced apartment in A", [], PropertyType.APARTMENT),
        ("Entire home/apt", "Entire loft in A", ["Studio"], PropertyType.STUDIO),
        ("Entire home/apt", "Entire guesthouse in A", [], PropertyType.OTHER),
        ("Entire home/apt", "Entire guesthouse in A", ["Studio"], PropertyType.STUDIO),
        ("Entire home/apt", "Entire cabin in A", ["Studio"], PropertyType.SINGLE_FAMILY),
        ("", "Room in bed and breakfast in A", [], PropertyType.ROOM),
        ("", "", [], PropertyType.OTHER),
    ],
)
def test_property_types(
    room_type: str, sub_title: str, sizes: list[str], expected: PropertyType
) -> None:
    item = minimal(roomType=room_type, subDescription={"title": sub_title, "items": sizes})

    assert one(item).property_type is expected


@pytest.mark.parametrize(
    ("sizes", "beds", "baths"),
    [
        (["2 guests", "2 bedrooms", "3 beds", "2.5 baths"], 2, 2.5),
        (["1 bedroom", "1 private bath"], 1, 1.0),
        (["Studio", "Half-bath"], 0, 0.5),
        (["1 bedroom", "Shared half-bath"], 1, 0.5),
        (["1 shared half-bath"], None, 0.5),
        (["4 guests", 3, None, ""], None, None),
        ("2 bedrooms", None, None),
    ],
)
def test_sizes(sizes: object, beds: int | None, baths: float | None) -> None:
    listing = one(minimal(subDescription={"items": sizes}))

    assert (listing.beds, listing.baths) == (beds, baths)


def test_odd_values_are_ignored_not_fatal() -> None:
    item = minimal(
        url="/rooms/1",
        title=" - Rooms for Rent in X - Airbnb",
        coordinates="40,-89",
        subDescription="Entire home",
        amenities=[
            "Wifi",
            {
                "title": "Basics",
                "values": [{"title": "", "available": True}, "Heating", {"title": "Iron"}],
            },
        ],
        images=["https://a0.muscache.com/pictures/x.jpeg", {"caption": "no url"}, 7],
    )

    listing = one(item)

    assert str(listing.url) == "https://www.airbnb.com/rooms/1"
    assert listing.title == "Airbnb stay"
    assert (listing.lat, listing.address.city) == (None, None)
    assert listing.amenities == frozenset()  # "Iron" isn't marked available
    assert [str(url) for url in listing.images] == ["https://a0.muscache.com/pictures/x.jpeg"]


def test_a_place_without_commas_is_the_city() -> None:
    sub = {"title": "Entire villa in Exampleville"}

    assert one(minimal(subDescription=sub)).address.city == "Exampleville"


# --- Criteria → actor input -----------------------------------------------------------


def test_the_country_comes_from_the_location_subtitle() -> None:
    item = minimal(
        locationSubtitle="Exampleville, Texas, United States",
        subDescription={"title": "Entire home in Exampleville, Texas"},
    )

    assert (one(item).address.city, one(item).address.region) == ("Exampleville", "Texas")


def test_a_live_run_maps_as_recorded() -> None:
    listings = [one(item) for item in fixture("dataset_live.json")]

    summary = [
        (
            listing.source_id,
            listing.price,
            listing.beds,
            listing.baths,
            listing.property_type,
            listing.address.region,
        )
        for listing in listings
    ]
    monthly = RentPeriod.MONTHLY
    assert summary == [
        (
            "900000001",
            Price(amount=usd(6883), period=monthly),
            4,
            2.5,
            PropertyType.SINGLE_FAMILY,
            "Texas",
        ),
        (
            "900000002",
            Price(amount=usd(1028), period=monthly),
            None,
            None,
            PropertyType.ROOM,
            "Texas",
        ),
        (
            "900000003",
            Price(amount=usd(3569), period=monthly),
            1,
            1.0,
            PropertyType.APARTMENT,
            "Texas",
        ),
    ]
    home = listings[0]
    assert home.title == "Executive 4BR King and Queens 2 living spaces"
    assert home.available_from == WINDOW.check_in
    assert len(home.images) == 3
    assert {"kitchen", "long term stays allowed"} <= home.amenities
    assert not any({"host", "coHosts"} & listing.raw.keys() for listing in listings)


def test_input_asks_for_a_month_from_the_earliest_move_in_and_supported_filters() -> None:
    criteria = Criteria(
        price_min=usd(1000),
        price_max=usd(3000),
        min_beds=2,
        min_baths=1.5,
        move_in_earliest=date(2026, 11, 15),
        lease_min_months=3,
    )

    run_input = build_input(criteria, locations=("Exampleville, IL",), max_results=50, today=TODAY)

    assert run_input == {
        "locationQueries": ["Exampleville, IL"],
        "checkIn": "2026-11-15",
        "checkOut": "2027-02-15",
        "currency": "USD",
        "locale": "en-US",
        "maxResults": 50,
        "enrichUserProfiles": False,
        "minBedrooms": 2,
        "minBathrooms": 1,
    }
    assert window_of(run_input) == StayWindow(date(2026, 11, 15), date(2027, 2, 15))


def test_input_leaves_out_what_criteria_leave_open() -> None:
    criteria = Criteria(min_beds=0, min_baths=0.5, move_in_earliest=date(2026, 1, 1))

    run_input = build_input(criteria, locations=("A", "B"), max_results=10, today=TODAY)

    # A past move-in date starts the stay today. Price is never sent.
    assert (run_input["checkIn"], run_input["checkOut"]) == ("2026-10-07", "2026-11-07")
    assert {"priceMin", "priceMax", "minBedrooms", "minBathrooms"}.isdisjoint(run_input)


@pytest.mark.parametrize(
    ("check_in", "months", "check_out"),
    [
        (date(2027, 1, 31), 1, date(2027, 2, 28)),  # clamped: still 28 nights
        (date(2027, 1, 31), 2, date(2027, 3, 31)),
        (date(2026, 12, 31), 2, date(2027, 2, 28)),
        (date(2027, 2, 1), 1, date(2027, 3, 1)),  # exactly 28 nights
        (date(2026, 11, 30), 12, date(2027, 11, 30)),
    ],
)
def test_stay_windows(check_in: date, months: int, check_out: date) -> None:
    criteria = Criteria(move_in_earliest=check_in, lease_min_months=months)

    window = stay_window(criteria, today=TODAY)

    assert (window.check_in, window.check_out) == (check_in, check_out)
    assert window.nights >= 28


# --- A whole search, against a faked Apify ------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401 - overrides
    entry = {"api_key": SecretStr(TOKEN), "search_locations": ("Exampleville, IL",), **overrides}
    return SourcesSettings(requests_per_minute=600, adapters={"airbnb": AdapterOverrides(**entry)})


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


def adapter(apify: Apify, source_settings: SourcesSettings | None = None) -> Airbnb:
    config = resolve_config(Airbnb, source_settings or settings())
    return Airbnb(
        config, transport=httpx2.MockTransport(apify), sleep=lambda _: None, today=lambda: TODAY
    )


def test_search_returns_parsed_listings_and_skips_bad_items() -> None:
    apify = Apify(items())

    listings = adapter(apify).search(Criteria(min_beds=1))

    assert [listing.source_id for listing in listings] == ["700000001", "700000002", "700000003"]
    # Priced over the window the run asked for.
    assert listings[0].price == Price(amount=Money(amount_minor=323987, currency="USD"))
    assert {listing.available_from for listing in listings} == {TODAY}
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/tri_angle~airbnb-scraper/runs")
    body = json.loads(start.content)
    assert (body["locationQueries"], body["checkIn"], body["checkOut"], body["maxResults"]) == (
        ["Exampleville, IL"],
        "2026-10-07",
        "2026-11-07",
        100,
    )
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
    config = resolve_config(Airbnb, settings(api_key=None))

    with pytest.raises(SourceError, match='set "api_key"'):
        Airbnb(config, transport=httpx2.MockTransport(Apify([]))).search(Criteria())


def test_discovered_and_disabled_until_it_has_a_key() -> None:
    registry = discover()
    assert registry.get("airbnb") is Airbnb

    by_name = {source.config.name: source for source in registry.configured(SourcesSettings())}
    assert by_name["airbnb"].config.enabled is False
    assert by_name["airbnb"].config.disabled_reason == (
        'needs an API key: set "api_key" for airbnb in SOURCES_ADAPTERS'
    )

    keyed = {source.config.name: source for source in registry.configured(settings(enabled=True))}
    assert (keyed["airbnb"].config.enabled, keyed["airbnb"].config.disabled_reason) == (True, None)


# --- Owner type ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, OwnerType.UNKNOWN),
        ({"host": {"id": "1"}}, OwnerType.INDIVIDUAL),
        ({"host": {"id": "1", "isSuperHost": True}}, OwnerType.INDIVIDUAL),
        ({"host": {"id": "1", "ratingCount": 999}}, OwnerType.INDIVIDUAL),
        ({"host": {"id": "1", "ratingCount": 1000}}, OwnerType.PROPERTY_MANAGER),
        ({"host": {"id": "1"}, "coHosts": [{"id": "2"}]}, OwnerType.INDIVIDUAL),
        ({"host": {"id": "1"}, "coHosts": [{"id": "2"}, {"id": "3"}]}, OwnerType.PROPERTY_MANAGER),
        ({"host": {"id": "1", "ratingCount": "lots"}, "coHosts": "two"}, OwnerType.INDIVIDUAL),
        # A host block beats the text; without one the text decides.
        ({"host": {"id": "1"}, "description": "Call our leasing office."}, OwnerType.INDIVIDUAL),
        ({"description": "Call our leasing office."}, OwnerType.PROPERTY_MANAGER),
    ],
)
def test_owner_type_from_the_host_block(fields: dict[str, Any], expected: OwnerType) -> None:
    assert one(minimal(**fields)).owner_type is expected


def test_the_host_is_read_for_the_owner_type_but_never_kept() -> None:
    listing = one(
        minimal(
            host={"id": "1", "name": "Jane Example", "ratingCount": 5000},
            coHosts=[{"id": "2", "name": "John Example"}],
        )
    )

    assert listing.owner_type is OwnerType.PROPERTY_MANAGER
    dumped = json.dumps(listing.raw)
    assert "Jane Example" not in dumped
    assert "John Example" not in dumped
    assert {"host", "coHosts"}.isdisjoint(listing.raw)


def test_the_live_run_owner_types() -> None:
    listings = [one(item) for item in fixture("dataset_live.json")]

    assert [(listing.source_id, listing.owner_type) for listing in listings] == [
        ("900000001", OwnerType.PROPERTY_MANAGER),  # three co-hosts
        ("900000002", OwnerType.INDIVIDUAL),
        ("900000003", OwnerType.PROPERTY_MANAGER),  # over ten thousand reviews
    ]
