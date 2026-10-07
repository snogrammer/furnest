"""Reading Craigslist's housing pages: search URLs in, search rows and postings out.

This module is pure: it builds URLs and reads HTML, and the adapter (``__init__``)
does the fetching. Only the standard library is used to read the pages, because
everything needed is either in a JSON-LD block or in a handful of stable classes.

A search page (``/search/area/<site>?cat=<category>&is_furnished=1&...``) has two
parallel views of the same results, in the same order:

- the static list, ``<li class="cl-static-search-result">``, with the post URL,
  title, price as shown and the neighbourhood;
- the JSON-LD ItemList ``#ld_searchpage_results``, with coordinates, beds, baths,
  city and state for each result, but no URL.

They are zipped by position. When the counts differ the JSON-LD is ignored rather
than risk pinning one post's coordinates on another.

A posting page (``/view/d/<slug>/<token>``) adds the description (``#postingbody``),
its own JSON-LD ``#ld_posting_data`` (street, postal code, coordinates), the
attribute chips (``2BR / 1Ba``, ``750ft2``, ``rent period: monthly``, ``house``,
``w/d in unit``, ``available oct 15``, ...), the photos and the posting time.

Fields map like this:

====================================  =================================================
page                                  NormalizedListing
====================================  =================================================
token, the last part of the post URL  ``source_id``
post URL                              ``url``
title                                 ``title``
``#postingbody`` text                 ``description``
price as shown, ``rent period``       ``price`` (monthly unless the posting says
                                      daily or weekly)
posting JSON-LD, else search JSON-LD  ``address`` (US), ``lat``/``lng``, ``beds``,
                                      ``baths``
``NBR / NBa``, ``Nft2`` chips         ``beds``, ``baths``, ``sqft``
housing chip, category ``roo``        ``property_type``
the other chips                       ``amenities``, as Craigslist words them
``available …`` chip                  ``available_from``
gallery photos                        ``images``
an email address in the description  ``contact.email``
====================================  =================================================

Every listing is ``furnished``: the search asks for Craigslist's own furnished
filter. Craigslist hides the poster's relay address behind ``/reply``, which
robots.txt disallows and a captcha guards, so it is never fetched. Unless the
poster wrote an email address into the description, the contact is
``platform_only`` and the property goes down the manual-contact path.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from furnest.domain import (
    Address,
    Contact,
    Criteria,
    NormalizedListing,
    Price,
    PropertyType,
    RentPeriod,
    normalize_property_type,
    parse_period,
    parse_price,
)
from furnest.sources.adapters._values import half, number, text, whole

SOURCE = "craigslist"
# Region searches live on the main host now; ``<site>.craigslist.org/search/<cat>``
# redirects here, so going straight there halves the requests.
SEARCH_ROOT = "https://www.craigslist.org/search/area"
_USD = "USD"
# Craigslist site names are lower-case letters and digits ("sfbay", "newyork").
SITE_NAME = re.compile(r"[a-z0-9]+")
APARTMENTS, SUBLETS, ROOMS = "apa", "sub", "roo"
# A posting's photos; the gallery shows each a few times at several sizes.
MAX_IMAGES = 20
# Text on a posting page that has been taken down but still answers 200.
_REMOVED_MARKERS = (
    "this posting has been deleted",
    "this posting has been flagged for removal",
    "this posting has expired",
)
# Chip wording that the amenity vocabulary would misread.
_AMENITY_WORDING = {
    "laundry in bldg": "laundry in building",
    # A hookup is not a washer; the vocabulary would read "w/d" as in-unit laundry.
    "w/d hookups": "laundry hookups",
}
# Housing chips, as Craigslist offers them in its "housing type" filter.
_HOUSING_TYPES = {
    "apartment": PropertyType.APARTMENT,
    "condo": PropertyType.CONDO,
    "cottage/cabin": PropertyType.SINGLE_FAMILY,
    "duplex": PropertyType.APARTMENT,
    "flat": PropertyType.APARTMENT,
    "house": PropertyType.SINGLE_FAMILY,
    "in-law": PropertyType.APARTMENT,
    "loft": PropertyType.APARTMENT,
    "townhouse": PropertyType.TOWNHOUSE,
    "manufactured": PropertyType.OTHER,
    "assisted living": PropertyType.OTHER,
    "land": PropertyType.OTHER,
}
_ROOM_CHIPS = frozenset({"private room", "shared room", "room"})
_BEDS_BATHS = re.compile(r"^(\d+)br\s*/\s*(\d+(?:\.\d+)?)ba$")
_SQFT = re.compile(r"^(\d[\d,]*)\s*ft\s*2?$")
_LABELLED = re.compile(r"^([a-z ]+):\s*(.+)$")
_AVAILABLE = re.compile(r"^available\s+(?:now|([a-z]{3})[a-z]*\s+(\d{1,2}))$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.IGNORECASE)
_IMAGE = re.compile(r"https://images\.craigslist\.org/[\w-]+_600x450\.jpg")
_POST_ID = re.compile(r"post id:\s*(\d+)")
_MONTHS = {
    month: index
    for index, month in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
# A move-in day this long before the posting date is read as next year's.
_YEAR_ROLLOVER = timedelta(days=60)
_SEARCH_LIST = '<ol class="cl-static-search-results"'
_SEARCH_ROW = re.compile(r'<li class="cl-static-search-result"[^>]*>(.*?)</li>', re.DOTALL)
_ROW_HREF = re.compile(r'<a href="([^"]+)"')
_ROW_TITLE = re.compile(r'<div class="title">(.*?)</div>', re.DOTALL)
_ROW_PRICE = re.compile(r'<div class="price">(.*?)</div>', re.DOTALL)
_ROW_HOOD = re.compile(r'<div class="location">(.*?)</div>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")


# --- Criteria → search URLs ---------------------------------------------------------


def categories(criteria: Criteria) -> tuple[str, ...]:
    """The housing categories to search for ``criteria``'s property types.

    Apartments/housing and sublets/temporary are searched unless only rooms are
    wanted, and rooms/shared unless rooms are ruled out.
    """
    allow = set(criteria.property_types_allow)
    if allow == {PropertyType.ROOM}:
        return (ROOMS,)
    if PropertyType.ROOM in criteria.property_types_deny or (
        allow and PropertyType.ROOM not in allow
    ):
        return (APARTMENTS, SUBLETS)
    return (APARTMENTS, SUBLETS, ROOMS)


def search_url(site: str, category: str, criteria: Criteria) -> str:
    """A newest-first furnished search of ``category`` on ``site``.

    The monthly price range (whole USD, rounded outwards) and minimum beds and baths
    (baths rounded down) are sent along. Craigslist filters on the price as posted,
    whatever its rent period, so a weekly price can slip through; matching compares
    monthly amounts afterwards.
    """
    if not SITE_NAME.fullmatch(site):
        msg = f"{site!r} is not a Craigslist site name (like 'sfbay')"
        raise ValueError(msg)
    params: dict[str, str | int] = {"cat": category, "is_furnished": 1, "sort": "date"}
    if criteria.price_min is not None and criteria.price_min.currency == _USD:
        params["min_price"] = math.floor(criteria.price_min.major)
    if criteria.price_max is not None and criteria.price_max.currency == _USD:
        params["max_price"] = math.ceil(criteria.price_max.major)
    # Rooms are one bedroom whatever the place has, so beds and baths don't apply.
    if category != ROOMS:
        if criteria.min_beds is not None and criteria.min_beds > 0:
            params["min_bedrooms"] = criteria.min_beds
        if criteria.min_baths is not None and criteria.min_baths >= 1:
            params["min_bathrooms"] = math.floor(criteria.min_baths)
    return f"{SEARCH_ROOT}/{site}?{urlencode(params)}"


# --- Search page → rows -------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    """One row of a search page, with its JSON-LD item (``{}`` if not matched)."""

    token: str
    url: str
    title: str
    price: str
    neighborhood: str
    category: str
    data: dict[str, Any] = field(default_factory=dict)


def parse_search(page: str, *, category: str) -> list[SearchResult]:
    """The rows of a search page, in page order.

    Raises ``ValueError`` when the page has no results list at all, so a changed
    layout fails the run instead of reading as "no listings".
    """
    if _SEARCH_LIST not in page:
        msg = "not a Craigslist search page: no results list"
        raise ValueError(msg)
    rows = [_row(match.group(1), category) for match in _SEARCH_ROW.finditer(page)]
    items = _search_items(page)
    if len(items) == len(rows):
        rows = [
            None if row is None else replace(row, data=item)
            for row, item in zip(rows, items, strict=True)
        ]
    return [row for row in rows if row is not None]


def _row(fragment: str, category: str) -> SearchResult | None:
    href = _ROW_HREF.search(fragment)
    url = html.unescape(href.group(1)) if href else ""
    token = post_token(url)
    title = _inline(_ROW_TITLE, fragment)
    if token is None or not title:
        return None
    return SearchResult(
        token=token,
        url=url,
        title=title,
        price=_inline(_ROW_PRICE, fragment),
        neighborhood=_inline(_ROW_HOOD, fragment),
        category=category,
    )


def post_token(url: str) -> str | None:
    """The post's id in its URL: ``/view/d/<slug>/<token>`` or ``.../<id>.html``."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (
        host == "craigslist.org" or host.endswith(".craigslist.org")
    ):
        return None
    last = parts.path.rstrip("/").rpartition("/")[2].removesuffix(".html")
    return last if re.fullmatch(r"[A-Za-z0-9]+", last) else None


def _inline(pattern: re.Pattern[str], fragment: str) -> str:
    found = pattern.search(fragment)
    return _collapse(html.unescape(_TAG.sub(" ", found.group(1)))) if found else ""


def _search_items(page: str) -> list[dict[str, Any]]:
    data = _json_ld(page, "ld_searchpage_results")
    elements = data.get("itemListElement")
    if not isinstance(elements, list):
        return []
    items: list[dict[str, Any]] = []
    for element in elements:
        item = element.get("item") if isinstance(element, dict) else None
        items.append(item if isinstance(item, dict) else {})
    return items


def _json_ld(page: str, element_id: str) -> dict[str, Any]:
    found = re.search(
        rf'<script[^>]*id="{element_id}"[^>]*>(.*?)</script>', page, re.DOTALL | re.IGNORECASE
    )
    if found is None:
        return {}
    try:
        data = json.loads(found.group(1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --- Posting page → detail ----------------------------------------------------------


@dataclass(frozen=True)
class Detail:
    """What a posting page adds to its search row."""

    description: str
    # Attribute chips, lower-cased, in page order.
    attributes: tuple[str, ...]
    data: dict[str, Any]
    images: tuple[str, ...] = ()
    posted: date | None = None
    post_id: str | None = None
    email: str | None = None


def parse_detail(page: str) -> Detail | None:
    """The posting on ``page``, or ``None`` if it has been taken down.

    Raises ``ValueError`` when the page is neither a posting nor a removal notice.
    """
    reader = _PostingReader()
    reader.feed(page)
    reader.close()
    if reader.body is None:
        lowered = page.lower()
        if any(marker in lowered for marker in _REMOVED_MARKERS):
            return None
        msg = "not a Craigslist posting: no posting body"
        raise ValueError(msg)
    description = _clean_body(reader.body)
    email = _EMAIL.search(description)
    post_id = _POST_ID.search(page)
    return Detail(
        description=description,
        attributes=tuple(a for a in (_collapse(t).lower() for t in reader.attributes) if a),
        data=_json_ld(page, "ld_posting_data"),
        images=tuple(dict.fromkeys(_IMAGE.findall(page)))[:MAX_IMAGES],
        posted=_posted(reader.times),
        post_id=post_id.group(1) if post_id else None,
        email=email.group(0) if email else None,
    )


class _PostingReader(HTMLParser):
    """Collects the posting body, the attribute chips and the ``<time>`` stamps.

    Each open element is kept on a stack with what it captures, so text inside a
    nested element still lands in the chip or body it belongs to. The print-only
    QR-code block inside the body is skipped.
    """

    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] | None = None
        self.attributes: list[str] = []
        self.times: list[str] = []
        # (tag, what it captures): "body", an index into attributes, "skip" or None.
        self._stack: list[tuple[str, str | int | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "time" and values.get("datetime"):
            self.times.append(values["datetime"] or "")
        if tag == "br":
            self._text("\n")
        if tag in self._VOID:
            return
        capture: str | int | None = None
        if values.get("id") == "postingbody" and self.body is None:
            self.body, capture = [], "body"
        elif "print-information" in classes:
            capture = "skip"
        elif "attr" in classes and not self._inside("body"):
            self.attributes.append("")
            capture = len(self.attributes) - 1
        self._stack.append((tag, capture))
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li"}:
            self._text("\n")

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._stack) - 1, -1, -1):
            if self._stack[depth][0] == tag:
                del self._stack[depth:]
                break
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li"}:
            self._text("\n")

    def handle_data(self, data: str) -> None:
        self._text(data)

    def _inside(self, capture: str) -> bool:
        return any(c == capture for _, c in self._stack)

    def _text(self, data: str) -> None:
        if self._inside("skip"):
            return
        for _, capture in self._stack:
            if capture == "body" and self.body is not None:
                self.body.append(data)
            elif isinstance(capture, int):
                self.attributes[capture] += data


def _clean_body(parts: list[str]) -> str:
    lines = (_collapse(line) for line in "".join(parts).splitlines())
    # Runs of blank lines (from <br> and block elements) become one paragraph break.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _collapse(value: str) -> str:
    return _SPACE.sub(" ", value.replace("\n", " ")).strip()


def _posted(times: list[str]) -> date | None:
    for stamp in times:
        try:
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z").date()
        except ValueError:
            continue
    return None


# --- Search row + detail → NormalizedListing ----------------------------------------


@dataclass(frozen=True)
class _Chips:
    beds: int | None = None
    baths: float | None = None
    sqft: int | None = None
    period: RentPeriod | None = None
    available: tuple[int, int] | None = None
    available_now: bool = False
    housing: PropertyType | None = None
    room: bool = False
    amenities: frozenset[str] = frozenset()
    labelled: dict[str, str] = field(default_factory=dict)


def to_listing(result: SearchResult, detail: Detail | None) -> NormalizedListing:
    """Map a search row, completed by its posting when there is one.

    Raises ``ValueError`` (pydantic's included) if the row is unusable.
    """
    chips = _chips(detail.attributes if detail else ())
    posting = detail.data if detail else {}
    beds = (
        chips.beds
        if chips.beds is not None
        else whole(_first(posting, result.data, "numberOfBedrooms"))
    )
    baths = (
        chips.baths
        if chips.baths is not None
        else half(_first(posting, result.data, "numberOfBathroomsTotal"))
    )
    lat = number(_first(posting, result.data, "latitude"))
    lng = number(_first(posting, result.data, "longitude"))
    if lat is None or lng is None:
        lat = lng = None
    email = detail.email if detail else None
    return NormalizedListing(
        source=SOURCE,
        source_id=result.token,
        url=result.url,  # type: ignore[arg-type]
        title=result.title,
        description=detail.description if detail else "",
        price=_price(result.price, chips.period),
        address=_address(posting, result.data),
        lat=lat,
        lng=lng,
        beds=beds,
        baths=baths,
        sqft=chips.sqft,
        property_type=_property_type(result, chips, beds),
        furnished=True,
        amenities=chips.amenities,
        available_from=_available_from(chips, detail.posted if detail else None),
        images=detail.images if detail else (),  # type: ignore[arg-type]
        contact=Contact(email=email) if email else Contact(platform_only=True),
        raw=_raw(result, detail, chips),
    )


def _chips(attributes: tuple[str, ...]) -> _Chips:
    beds = baths = sqft = period = available = housing = None
    available_now = room = False
    amenities: set[str] = set()
    labelled: dict[str, str] = {}
    for chip in attributes:
        if found := _BEDS_BATHS.match(chip):
            beds, baths = whole(found.group(1)), half(found.group(2))
        elif found := _SQFT.match(chip):
            sqft = whole(found.group(1).replace(",", "")) or None
        elif found := _AVAILABLE.match(chip):
            month = found.group(1)
            if month is None:
                available_now = True
            elif month in _MONTHS:
                available = (_MONTHS[month], int(found.group(2)))
        elif found := _LABELLED.match(chip):
            label, value = found.group(1).strip(), found.group(2).strip()
            labelled[label] = value
            if label == "rent period":
                period = parse_period(value)
        elif chip in _ROOM_CHIPS:
            room = True
        elif chip in _HOUSING_TYPES:
            housing = _HOUSING_TYPES[chip]
        else:
            amenities.add(_AMENITY_WORDING.get(chip, chip))
    return _Chips(
        beds=beds,
        baths=baths,
        sqft=sqft,
        period=period,
        available=available,
        available_now=available_now,
        housing=housing,
        room=room,
        amenities=frozenset(amenities),
        labelled=labelled,
    )


def _first(posting: dict[str, Any], item: dict[str, Any], key: str) -> object:
    value = posting.get(key)
    return value if value not in (None, "") else item.get(key)


def _price(shown: str, period: RentPeriod | None) -> Price | None:
    return parse_price(shown or None, currency=_USD, default_period=period or RentPeriod.MONTHLY)


def _address(posting: dict[str, Any], item: dict[str, Any]) -> Address:
    def part(key: str) -> str | None:
        for data in (posting, item):
            address = data.get("address")
            if isinstance(address, dict) and (value := text(address.get(key))):
                return value
        return None

    return Address(
        line1=part("streetAddress"),
        city=part("addressLocality"),
        region=part("addressRegion"),
        postal_code=part("postalCode"),
    )


def _property_type(result: SearchResult, chips: _Chips, beds: int | None) -> PropertyType:
    if chips.room or result.category == ROOMS:
        return PropertyType.ROOM
    found = chips.housing
    if found is None:
        # The JSON-LD type: "Apartment", "House", "SingleFamilyResidence", ...
        found = normalize_property_type(text(result.data.get("@type")))
    if found is PropertyType.APARTMENT and beds == 0:
        return PropertyType.STUDIO
    return found


def _available_from(chips: _Chips, posted: date | None) -> date | None:
    # "Available now" is pinned to the posting date, not today, so a listing doesn't
    # read as changed on every run.
    if chips.available_now:
        return posted
    if chips.available is None or posted is None:
        return None
    month, day = chips.available
    try:
        when = date(posted.year, month, day)
        if when < posted - _YEAR_ROLLOVER:
            when = date(posted.year + 1, month, day)
    except ValueError:  # "feb 30", or feb 29 in the wrong year
        return None
    return when


def _raw(result: SearchResult, detail: Detail | None, chips: _Chips) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "category": result.category,
        "price": result.price,
        "neighborhood": result.neighborhood,
        "search": result.data,
    }
    if detail is not None:
        raw["posting"] = detail.data
        raw["attributes"] = list(detail.attributes)
        if detail.post_id:
            raw["post_id"] = detail.post_id
        if detail.posted:
            raw["posted"] = detail.posted.isoformat()
    if chips.labelled:
        raw["labelled"] = chips.labelled
    return raw
