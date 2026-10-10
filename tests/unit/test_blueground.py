"""The Blueground adapter: criteria → city-page URLs, pages → listings, and a whole
search against faked city pages. Runs offline on tests/fixtures/blueground."""

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, Money, OwnerType, PropertyType, RentPeriod
from furnest.sources import SourceBlockedError, SourceError, discover, resolve_config
from furnest.sources.adapters.airbnb.parse import StayWindow
from furnest.sources.adapters.blueground import Blueground
from furnest.sources.adapters.blueground.parse import (
    CityPage,
    blueground_window,
    city_url,
    parse_city_page,
    to_listing,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "blueground"
TODAY = date(2026, 10, 9)
WINDOW = StayWindow(date(2026, 11, 1), date(2026, 12, 1))
ROBOTS = "User-agent: *\nDisallow: /book\nDisallow: /users\n"


def page(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text()


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


def with_state(html: str, change: Any) -> str:  # noqa: ANN401
    """``html`` with its state passed through ``change``."""
    marker = "window.__INITIAL_STATE__="
    start = html.index(marker) + len(marker)
    state, length = json.JSONDecoder().raw_decode(html[start:])
    change(state)
    return html[:start] + json.dumps(state) + html[start + length :]


# --- URLs -------------------------------------------------------------------------


def test_window_is_a_month_from_today_by_default() -> None:
    assert blueground_window(Criteria(), today=TODAY) == StayWindow(TODAY, date(2026, 11, 9))


def test_window_is_never_under_thirty_nights() -> None:
    window = blueground_window(Criteria(), today=date(2027, 2, 1))
    assert window == StayWindow(date(2027, 2, 1), date(2027, 3, 3))
    criteria = Criteria(move_in=date(2026, 12, 1), move_out=date(2027, 3, 1))
    assert blueground_window(criteria, today=TODAY) == StayWindow(
        date(2026, 12, 1), date(2027, 3, 1)
    )


def test_city_url() -> None:
    assert city_url("chicago", WINDOW) == (
        "https://www.theblueground.com/furnished-apartments-chicago-usa"
        "?checkIn=2026-11-01&checkOut=2026-12-01"
    )
    assert city_url("new-york-usa", WINDOW, page=3).endswith(
        "furnished-apartments-new-york-usa?checkIn=2026-11-01&checkOut=2026-12-01&page=3"
    )


# --- Parsing ----------------------------------------------------------------------


def test_priced_page_maps_to_listings() -> None:
    parsed = parse_city_page(page("city_priced"))
    assert parsed.region == "IL"
    assert len(parsed.rows) == 4

    listing = to_listing(parsed.rows[0], parsed)
    assert listing.source == "blueground"
    assert listing.source_id == "chi-1397"
    assert str(listing.url) == "https://www.theblueground.com/p/furnished-apartments/chi-1397"
    assert listing.title == "61 West Erie St"
    assert listing.price is not None
    assert listing.price.amount == usd(2525)
    assert listing.price.period is RentPeriod.MONTHLY
    assert listing.beds == 1
    assert listing.baths is not None
    assert listing.sqft is not None
    assert (listing.lat, listing.lng) == (39.81, -89.68)
    assert listing.address.city == "Chicago"
    assert listing.address.region == "IL"
    assert listing.address.neighborhood == "Near North Side"
    assert listing.address.line1 == "61 West Erie St"
    assert listing.available_from == date(2026, 11, 1)
    assert listing.furnished is True
    assert listing.owner_type is OwnerType.PROPERTY_MANAGER
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.contact.platform_only
    assert listing.min_lease_months == 1
    assert "utilities included" in listing.amenities
    assert 0 < len(listing.images) <= 20
    assert all(
        str(image).startswith("https://photos2.theblueground.com/") for image in listing.images
    )
    assert listing.raw["code"] == "1397"


def test_building_prefix_is_dropped_from_the_street() -> None:
    parsed = parse_city_page(page("city_priced"))
    listing = to_listing(parsed.rows[2], parsed)
    assert listing.title == "Left Bank, 300 North Canal St"
    assert listing.address.line1 == "300 North Canal St"


def test_unpriced_page_has_no_price_never_zero() -> None:
    parsed = parse_city_page(page("city_unpriced"))
    assert parsed.total == 649
    listings = [to_listing(row, parsed) for row in parsed.rows]
    assert [listing.price for listing in listings] == [None] * 4
    assert listings[0].source_id == "chi-819"
    assert {"gym", "pool", "utilities included"} <= listings[0].amenities


def test_json_ld_fills_what_a_row_lacks() -> None:
    parsed = parse_city_page(page("city_unpriced"))
    row = dict(parsed.rows[0])
    for key in ("bedrooms", "bathrooms", "lotSize", "photos", "address"):
        row.pop(key)
    listing = to_listing(row, parsed)
    assert listing.beds == 1
    assert listing.baths == 1
    assert listing.sqft == 702
    assert (listing.lat, listing.lng) == (39.79, -89.66)
    assert [str(image) for image in listing.images] == [
        "https://photos2.theblueground.com/736/pg25338-o-65413652-8af8-e076-6a51-5bad3c029ffc.jpg"
    ]


def test_studio() -> None:
    parsed = parse_city_page(page("city_priced"))
    listing = to_listing(dict(parsed.rows[0], bedrooms=0), parsed)
    assert listing.beds == 0
    assert listing.property_type is PropertyType.STUDIO


def test_row_without_a_usable_path_is_rejected() -> None:
    parsed = parse_city_page(page("city_priced"))
    with pytest.raises(ValueError, match="path"):
        to_listing(dict(parsed.rows[0], path="book/123"), parsed)
    with pytest.raises(ValueError, match="name"):
        to_listing(dict(parsed.rows[0], name=" "), CityPage(rows=[], total=0))


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>nothing here</body></html>",
        "<script>window.__INITIAL_STATE__={broken</script>",
        "<script>window.__INITIAL_STATE__={}</script>",
        '<script>window.__INITIAL_STATE__={"properties": {"allProperties": {}}}</script>',
    ],
)
def test_pages_without_a_home_list_are_rejected(html: str) -> None:
    with pytest.raises(ValueError, match="Blueground"):
        parse_city_page(html)


def test_empty_home_list_is_no_results() -> None:
    html = with_state(
        page("city_priced"), lambda s: s["properties"]["allProperties"].update(main=[])
    )
    assert parse_city_page(html).rows == []


# --- Adapter ----------------------------------------------------------------------


def config(**overrides: Any) -> Any:  # noqa: ANN401
    settings = SourcesSettings(
        adapters={"blueground": AdapterOverrides(**{"requests_per_minute": 6000, **overrides})}
    )
    return resolve_config(Blueground, settings)


class FakeBlueground:
    """City pages: ``pages`` maps a city path to its pages' HTML, in order."""

    def __init__(self, pages: dict[str, list[str]] | None = None, status: int = 200) -> None:
        self.pages = pages or {"furnished-apartments-chicago-usa": [page("city_priced")]}
        self.status = status
        self.requests: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.requests.append(f"{url.path}?{url.query.decode()}" if url.query else url.path)
        if url.path == "/robots.txt":
            return httpx2.Response(200, text=ROBOTS)
        pages = self.pages.get(url.path.lstrip("/"))
        if pages is None or self.status != 200:
            return httpx2.Response(self.status if self.status != 200 else 404, text="not found")
        number = int(parse_qs(urlsplit(str(url)).query).get("page", ["1"])[0])
        return httpx2.Response(200, text=pages[number - 1])

    def city_requests(self) -> list[str]:
        return [r for r in self.requests if r != "/robots.txt"]


def adapter(fake: FakeBlueground, **overrides: Any) -> Blueground:  # noqa: ANN401
    overrides.setdefault("search_locations", ("chicago",))
    return Blueground(
        config(**overrides),
        transport=httpx2.MockTransport(fake),
        sleep=lambda _: None,
        today=lambda: TODAY,
    )


def numbered(html: str, offset: int, total: int) -> str:
    """``html`` with its homes renumbered from ``offset`` and ``total`` homes in all."""

    def change(state: dict[str, Any]) -> None:
        state["properties"]["meta"]["totalItems"] = total
        for row in state["properties"]["allProperties"]["main"]:
            row["path"] = f"p/furnished-apartments/chi-{int(row['code']) + offset}"

    return with_state(html, change)


def test_registered_and_off_by_default() -> None:
    assert discover().get("blueground") is Blueground
    assert Blueground.implemented
    assert resolve_config(Blueground, SourcesSettings()).enabled is False


def test_search_asks_for_the_stay_and_maps_the_page() -> None:
    fake = FakeBlueground()
    listings = adapter(fake).search(Criteria(move_in=date(2026, 11, 1)))

    assert [listing.source_id for listing in listings] == [
        "chi-1397",
        "chi-1411",
        "chi-1388",
        "chi-1431",
    ]
    assert fake.city_requests() == [
        "/furnished-apartments-chicago-usa?checkIn=2026-11-01&checkOut=2026-12-01"
    ]
    assert listings[0].price is not None


def test_max_results_caps_the_listings() -> None:
    assert len(adapter(FakeBlueground(), max_results=2).search(Criteria())) == 2


def test_later_pages_are_fetched_until_max_results() -> None:
    first = numbered(page("city_priced"), 0, total=40)
    second = numbered(page("city_priced"), 1000, total=40)
    fake = FakeBlueground({"furnished-apartments-chicago-usa": [first, second]})

    # 18 homes to a page, so 20 results take two pages; a third isn't asked for.
    listings = adapter(fake, max_results=20).search(Criteria())

    assert len(listings) == 8
    assert listings[-1].source_id == "chi-2431"
    assert len(fake.city_requests()) == 2
    assert fake.city_requests()[1].endswith("&page=2")


def test_paging_stops_when_the_city_has_no_more() -> None:
    html = numbered(page("city_priced"), 0, total=4)
    fake = FakeBlueground({"furnished-apartments-chicago-usa": [html]})
    assert len(adapter(fake, max_results=50).search(Criteria())) == 4
    assert len(fake.city_requests()) == 1


def test_cities_are_taken_in_turn_and_repeats_dropped() -> None:
    def pages(*offsets: int) -> list[str]:
        return [numbered(page("city_priced"), offset, total=36) for offset in offsets]

    fake = FakeBlueground(
        {
            "furnished-apartments-chicago-usa": pages(0, 1000),
            "furnished-apartments-new-york-usa": pages(0, 2000),
        }
    )
    listings = adapter(fake, search_locations=("chicago", "new-york"), max_results=50).search(
        Criteria()
    )
    assert len(listings) == 12
    assert [r.split("?")[0].split("-")[2] for r in fake.city_requests()] == [
        "chicago",
        "new",
        "chicago",
        "new",
    ]


def test_page_budget_still_covers_every_city_once() -> None:
    first = numbered(page("city_priced"), 0, total=400)
    fake = FakeBlueground(
        {
            "furnished-apartments-chicago-usa": [first, first],
            "furnished-apartments-boston-usa": [first, first],
        }
    )
    # One request a minute over a 60 s run: nothing past each city's first page.
    adapter(
        fake,
        search_locations=("chicago", "boston"),
        requests_per_minute=60,
        run_timeout_seconds=1,
        max_results=100,
    ).search(Criteria())
    assert len(fake.city_requests()) == 2


def test_unreadable_page_fails_the_run() -> None:
    fake = FakeBlueground({"furnished-apartments-chicago-usa": ["<html>new layout</html>"]})
    with pytest.raises(SourceError, match="layout"):
        adapter(fake).search(Criteria())


def test_unknown_city_fails_the_run() -> None:
    with pytest.raises(SourceError) as caught:
        adapter(FakeBlueground(), search_locations=("nowhere",)).search(Criteria())
    assert caught.value.status == 404


def test_block_fails_the_run() -> None:
    with pytest.raises(SourceBlockedError):
        adapter(FakeBlueground(status=403)).search(Criteria())


def test_robots_disallowing_the_city_page_fails_the_run() -> None:
    fake = FakeBlueground()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, text="User-agent: *\nDisallow: /furnished-apartments\n")
        return fake(request)

    blueground = Blueground(
        config(search_locations=("chicago",)),
        transport=httpx2.MockTransport(handler),
        sleep=lambda _: None,
        today=lambda: TODAY,
    )
    with pytest.raises(SourceBlockedError):
        blueground.search(Criteria())
    assert fake.city_requests() == []


def test_missing_and_malformed_cities_fail_the_run() -> None:
    with pytest.raises(SourceError, match="search_locations"):
        adapter(FakeBlueground(), search_locations=()).search(Criteria())
    with pytest.raises(SourceError, match="city slugs"):
        adapter(FakeBlueground(), search_locations=("New York, NY",)).search(Criteria())


def test_unreadable_rows_are_skipped() -> None:
    def change(state: dict[str, Any]) -> None:
        state["properties"]["allProperties"]["main"][0]["path"] = "book/1"

    fake = FakeBlueground(
        {"furnished-apartments-chicago-usa": [with_state(page("city_priced"), change)]}
    )
    assert next(iter(adapter(fake).search(Criteria()))).source_id == "chi-1411"


def test_photo_captions_become_amenities() -> None:
    parsed = parse_city_page(page("city_priced"))
    photos = [
        {"url": "https://photos2.theblueground.com/x.jpg", "captionKey": "washerDryerInApartment"}
    ]
    listing = to_listing(dict(parsed.rows[0], photos=photos, utilitiesIncludedInRent=False), parsed)
    assert listing.amenities == frozenset({"in-unit laundry"})
