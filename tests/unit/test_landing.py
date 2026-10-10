"""The Landing adapter: criteria → API URLs, JSON → listings, and a whole search against
a faked Landing API. Runs offline on tests/fixtures/landing."""

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, Money, OwnerType, PropertyType, RentPeriod
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.airbnb.parse import StayWindow
from furnest.sources.adapters.landing import DetailCache, Landing
from furnest.sources.adapters.landing.parse import (
    landing_window,
    parse_detail,
    parse_markets,
    parse_search,
    quote_url,
    search_url,
    to_listing,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "landing"
TODAY = date(2026, 10, 9)
WINDOW = StayWindow(date(2026, 11, 8), date(2027, 2, 6))
SLUG = "apartment-in-birmingham-al-the-ellison-347"
ROBOTS = "User-agent: *\nAllow: /api/public\nDisallow: /api/\n"


def load(name: str) -> Any:  # noqa: ANN401
    return json.loads((FIXTURES / f"{name}.json").read_text())


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


# --- URLs -------------------------------------------------------------------------


def test_window_is_a_month_from_today_by_default() -> None:
    assert landing_window(Criteria(), today=TODAY) == StayWindow(TODAY, date(2026, 11, 9))


def test_window_follows_move_in_and_lease_and_is_clamped_to_a_year() -> None:
    criteria = Criteria(move_in=date(2026, 12, 1), move_out=date(2027, 3, 1))
    assert landing_window(criteria, today=TODAY) == StayWindow(date(2026, 12, 1), date(2027, 3, 1))
    far = Criteria(move_in=date(2028, 1, 1), move_out=date(2028, 2, 1))
    window = landing_window(far, today=TODAY)
    assert window.check_in == date(2027, 10, 9)
    assert window.nights >= 28


def test_search_url_carries_criteria() -> None:
    criteria = Criteria(min_beds=1, price_max=usd(4000))
    query = parse_qs(urlsplit(search_url("austin", WINDOW, criteria)).query)
    assert query == {
        "market": ["austin"],
        "check_in": ["2026-11-08"],
        "check_out": ["2027-02-06"],
        "bedrooms": ["1"],
        "max_price": ["4000"],
    }


def test_search_url_omits_unset_criteria() -> None:
    query = parse_qs(urlsplit(search_url("austin", WINDOW, Criteria())).query)
    assert set(query) == {"market", "check_in", "check_out"}


def test_quote_url() -> None:
    assert quote_url(SLUG, WINDOW).endswith(
        f"/homes/{SLUG}/quote?check_in=2026-11-08&check_out=2027-02-06"
    )


# --- Parsing ----------------------------------------------------------------------


def test_parse_markets_collects_market_and_city_slugs() -> None:
    slugs = parse_markets(load("markets"))
    assert {"akron", "akron-oh", "asheville", "arden-nc"} <= slugs


def test_parse_markets_rejects_other_payloads() -> None:
    with pytest.raises(ValueError, match="markets"):
        parse_markets({"error": "nope"})


def test_parse_search_returns_rows() -> None:
    rows = parse_search(load("search"))
    assert rows[0]["slug"] == SLUG
    with pytest.raises(ValueError, match="homes"):
        parse_search([])


def test_listing_from_row_alone() -> None:
    row = parse_search(load("search"))[0]
    listing = to_listing(row, None, window=WINDOW)

    assert listing.source == "landing"
    assert listing.source_id == SLUG
    assert str(listing.url).startswith("https://www.hellolanding.com/homes/")
    assert listing.title == "The Ellison 347"
    assert listing.price is not None
    assert (listing.price.amount, listing.price.period) == (usd(2798), RentPeriod.MONTHLY)
    assert (listing.beds, listing.baths, listing.sqft) == (2, 2.0, 877)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.owner_type is OwnerType.PROPERTY_MANAGER
    assert listing.furnished is True
    assert listing.address.line1 == "2000 2nd Ave S."
    assert (listing.address.city, listing.address.region, listing.address.postal_code) == (
        "Birmingham",
        "AL",
        "35233",
    )
    assert listing.lat == pytest.approx(33.51)
    assert listing.available_from == WINDOW.check_in
    assert listing.min_lease_months is None  # a 5-night minimum is no month
    assert listing.contact.platform_only is True
    assert len(listing.images) == 1


def test_listing_completed_by_home_and_quote() -> None:
    row = parse_search(load("search"))[0]
    detail = parse_detail(load("home"), load("quote"))
    listing = to_listing(row, detail, window=WINDOW)

    assert detail.available
    assert {"fitness center", "parking", "in unit laundry", "cats allowed", "dogs allowed"} <= (
        listing.amenities
    )
    assert len(listing.images) == 4
    quoted: Any = listing.raw["quote"]
    assert quoted["charge_schedule"]["total_monthly"] == 3291.14


def test_studio_and_nightly_price_and_unavailable_quote() -> None:
    row = dict(parse_search(load("search"))[0], bedrooms=0, is_price_nightly=True)
    row["display_price_with_discount"] = 100
    listing = to_listing(row, None, window=WINDOW)
    assert listing.property_type is PropertyType.STUDIO
    assert listing.price is not None
    assert listing.price.period is RentPeriod.NIGHTLY

    detail = parse_detail(load("home"), dict(load("quote"), available=False))
    assert not detail.available


def test_row_without_slug_is_rejected() -> None:
    with pytest.raises(ValueError, match="slug"):
        to_listing({"name": "x"}, None, window=WINDOW)


# --- Adapter ----------------------------------------------------------------------


def config(**overrides: Any) -> Any:  # noqa: ANN401
    settings = SourcesSettings(
        adapters={"landing": AdapterOverrides(**{"requests_per_minute": 6000, **overrides})}
    )
    return resolve_config(Landing, settings)


class FakeLanding:
    def __init__(self, *, quote: dict[str, Any] | None = None, gone: bool = False) -> None:
        self.requests: list[str] = []
        self.quote = quote or load("quote")
        self.gone = gone

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.requests.append(f"{url.path}?{url.query.decode()}" if url.query else url.path)
        if url.path == "/robots.txt":
            return httpx2.Response(200, text=ROBOTS)
        if url.path == "/api/public/markets":
            return httpx2.Response(200, json=load("markets"))
        if url.path == "/api/public/search":
            return httpx2.Response(200, json=load("search"))
        if url.path.endswith("/quote"):
            return httpx2.Response(404 if self.gone else 200, json=self.quote)
        if url.path.startswith("/api/public/homes/"):
            return httpx2.Response(404 if self.gone else 200, json=load("home"))
        return httpx2.Response(404)

    def count(self, fragment: str) -> int:
        return sum(fragment in r for r in self.requests)


def adapter(fake: FakeLanding, **overrides: Any) -> Landing:  # noqa: ANN401
    overrides.setdefault("search_locations", ("asheville",))
    return Landing(
        config(**overrides),
        transport=httpx2.MockTransport(fake),
        cache=DetailCache(),
        sleep=lambda _: None,
        today=lambda: TODAY,
    )


def test_registered_and_off_by_default() -> None:
    registry = discover()
    assert registry.get("landing") is Landing
    assert Landing.implemented
    assert resolve_config(Landing, SourcesSettings()).enabled is False


def test_search_maps_and_completes_rows() -> None:
    fake = FakeLanding()
    listings = adapter(fake, max_results=2).search(Criteria(min_beds=1))

    assert listings[0].source_id == SLUG
    assert len(listings) == 2
    assert fake.count("/api/public/markets") == 1
    assert fake.count("/api/public/search?market=asheville") == 1
    assert "bedrooms=1" in next(r for r in fake.requests if "/search" in r)
    assert fake.count("/quote") == 2
    assert "parking" in listings[0].amenities


def test_homes_are_fetched_once_per_stay_across_runs() -> None:
    fake = FakeLanding()
    landing = adapter(fake, max_results=1)
    landing.search(Criteria())
    landing.search(Criteria())
    assert fake.count("/quote") == 1
    assert fake.count("/api/public/search") == 2


def test_unavailable_or_gone_homes_are_dropped() -> None:
    unavailable = FakeLanding(quote=dict(load("quote"), available=False))
    assert adapter(unavailable, max_results=1).search(Criteria()) == []
    assert adapter(FakeLanding(gone=True), max_results=1).search(Criteria()) == []


def test_homes_past_the_request_budget_come_from_the_search_row() -> None:
    fake = FakeLanding()
    # 1 request/min over a 60 s run leaves nothing after markets and search.
    listings = adapter(fake, requests_per_minute=60, run_timeout_seconds=3).search(Criteria())
    assert len(listings) == 3
    assert fake.count("/quote") == 0
    assert listings[0].amenities == frozenset()


def test_missing_locations_fail_the_run() -> None:
    with pytest.raises(SourceError, match="search_locations"):
        adapter(FakeLanding(), search_locations=()).search(Criteria())


def test_malformed_and_unknown_locations_fail_the_run() -> None:
    with pytest.raises(SourceError, match="slugs"):
        adapter(FakeLanding(), search_locations=("Austin, TX",)).search(Criteria())
    with pytest.raises(SourceError, match="no market or city 'nowhere'"):
        adapter(FakeLanding(), search_locations=("nowhere",)).search(Criteria())


def test_unreadable_search_fails_the_run() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, text=ROBOTS)
        if request.url.path.endswith("/markets"):
            return httpx2.Response(200, json=load("markets"))
        return httpx2.Response(200, json={"unexpected": True})

    landing = Landing(
        config(search_locations=("asheville",)),
        transport=httpx2.MockTransport(handler),
        cache=DetailCache(),
        sleep=lambda _: None,
        today=lambda: TODAY,
    )
    with pytest.raises(SourceError, match="homes"):
        landing.search(Criteria())


def test_detail_cache_expires_and_evicts() -> None:
    now = [0.0]
    cache = DetailCache(max_entries=1, ttl_seconds=10, clock=lambda: now[0])
    detail = parse_detail(load("home"), load("quote"))
    cache.put(("a", "x", "y"), detail)
    cache.put(("b", "x", "y"), detail)
    assert cache.get(("a", "x", "y")) is None
    assert cache.get(("b", "x", "y")) is detail
    now[0] = 11
    assert cache.get(("b", "x", "y")) is None
    assert len(cache) == 0
