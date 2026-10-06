"""The match message payload: snapshot tests plus the safety and limit rules.

Each ``test_snapshot_*`` compares a payload with a JSON file in
``tests/fixtures/discord/``. After a deliberate change to the message, rewrite them
with ``FURNEST_UPDATE_SNAPSHOTS=1 pytest tests/unit/test_discord_embeds.py`` and
review the diff.
"""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from furnest.db.property_store import (
    ListingDetail,
    ListingSummary,
    PropertyDetail,
    PropertyState,
)
from furnest.domain import Money, NormalizedListing, PropertyStatus
from furnest.matching.scoring import BreakdownItem, MatchResult
from furnest.notify.custom_ids import DiscordAction, parse_custom_id
from furnest.notify.embeds import (
    LIMITS,
    dashboard_url,
    disabled_actions,
    embed_length,
    escape_markdown,
    match_message,
    truncate,
)

pytestmark = pytest.mark.unit

SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "discord"
BASE_URL = "https://furnest.example.com/"
AT = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
NAMES = {"example_source": "Example Source"}


def state(**changes: Any) -> PropertyState:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": 42,
        "status": PropertyStatus.SUGGESTED,
        "address": "123 Main St, Springfield, ST 00000",
        "lat": None,
        "lng": None,
        "score": 87,
        "criteria_version_id": 1,
        "snoozed_until": None,
        "manual_contact_required": False,
        "status_changed_at": AT,
        "created_at": AT,
    }
    values.update(changes)
    return PropertyState(**values)


def listing_detail(**changes: Any) -> ListingDetail:  # noqa: ANN401
    values: dict[str, Any] = {
        "source": "example_source",
        "source_id": "abc-1",
        "url": "https://listings.example.com/units/abc-1",
        "title": "Sunny furnished 2BR near the park",
        "images": [
            "https://img.example.com/abc-1/1.jpg",
            "https://img.example.com/abc-1/2.jpg",
        ],
        "beds": 2,
        "baths": 1.5,
        "first_seen": AT,
        "last_seen": AT,
    }
    values.update(changes)
    listing = NormalizedListing.model_validate(values)
    summary = ListingSummary(
        id=7,
        source=listing.source,
        url=str(listing.url),
        title=listing.title,
        monthly_price=Money.from_major("2400"),
        beds=listing.beds,
        baths=listing.baths,
        removed_at=None,
    )
    return ListingDetail(summary=summary, listing=listing, price_history=[])


MATCH = MatchResult(
    passed=True,
    score=87,
    breakdown=(
        BreakdownItem(label="Washer/dryer", points=10, max_points=10, reason="in unit"),
        BreakdownItem(label="Price fit", points=27.46, max_points=30, reason="under budget"),
        BreakdownItem(label="Parking", points=0, max_points=10, reason="unknown"),
        BreakdownItem(label="Extra beds", points=7.5, max_points=15, reason="1 extra"),
        BreakdownItem(label="Dishwasher", points=5, max_points=5, reason="listed"),
    ),
    criteria_version=1,
)


def detail(**changes: Any) -> PropertyDetail:  # noqa: ANN401
    values: dict[str, Any] = {
        "property": state(),
        "match": MATCH,
        "listings": [listing_detail()],
        "audit": [],
        "notes": [],
    }
    values.update(changes)
    return PropertyDetail(**values)


def build(d: PropertyDetail, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    return match_message(
        d, base_url=BASE_URL, source_name=lambda key: NAMES.get(key, key), **kwargs
    )


def assert_snapshot(name: str, payload: dict[str, Any]) -> None:
    path = SNAPSHOTS / f"{name}.json"
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("FURNEST_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.exists(), f"missing snapshot {path.name}; run with FURNEST_UPDATE_SNAPSHOTS=1"
    assert rendered == path.read_text(encoding="utf-8")


# --- Snapshots --------------------------------------------------------------------


def test_snapshot_full_match() -> None:
    assert_snapshot("match_full", build(detail()))


def test_snapshot_without_photo_match_or_price() -> None:
    bare = listing_detail(images=[], beds=None, baths=None)
    bare = replace(bare, summary=replace(bare.summary, monthly_price=None))
    d = detail(property=state(score=None, address=None), match=None, listings=[bare])

    assert_snapshot("match_minimal", build(d))


def test_snapshot_property_without_listings() -> None:
    d = detail(listings=[], property=state(status=PropertyStatus.UNAVAILABLE))

    assert_snapshot("match_no_listing", build(d))


def test_snapshot_hostile_text_and_disabled_buttons() -> None:
    title = "@everyone **HUGE** deal [click](https://evil.example.com) " + "x" * 300
    hostile = listing_detail(
        title=title,
        url="https://listings.example.com/units/(abc)?q=1",
        source="weird_source",
    )
    d = detail(
        property=state(status=PropertyStatus.APPROVED, address="*1* _Main_ St > `x`"),
        listings=[hostile],
    )

    payload = build(d, disabled={DiscordAction.APPROVE, DiscordAction.SNOOZE})

    assert_snapshot("match_hostile", payload)


def test_snapshot_manual_contact() -> None:
    d = detail(property=state(manual_contact_required=True))

    assert_snapshot("match_manual_contact", build(d))


def test_manual_contact_without_a_listing_has_no_contact_field() -> None:
    d = detail(listings=[], property=state(manual_contact_required=True))

    (embed,) = build(d)["embeds"]
    assert "Contact" not in [field["name"] for field in embed["fields"]]


# --- Rules ------------------------------------------------------------------------


def test_no_mentions_are_ever_allowed() -> None:
    assert build(detail())["allowed_mentions"] == {"parse": []}


def test_buttons_carry_parseable_custom_ids_in_order() -> None:
    (row,) = build(detail())["components"]
    parsed = [parse_custom_id(button["custom_id"]) for button in row["components"]]

    assert parsed == [(action, 42) for action in DiscordAction]
    assert all(not button["disabled"] for button in row["components"])


def test_top_breakdown_is_the_three_biggest_contributors() -> None:
    fields = {f["name"]: f["value"] for f in build(detail())["embeds"][0]["fields"]}

    assert fields["Why it matched"].splitlines() == [
        "• Price fit — 27.5/30 pts",
        "• Washer/dryer — 10/10 pts",
        "• Extra beds — 7.5/15 pts",
    ]


def test_breakdown_with_no_points_is_left_out() -> None:
    zero = MatchResult(
        passed=True,
        score=0,
        breakdown=(BreakdownItem(label="Parking", points=0, max_points=10, reason="none"),),
    )
    names = [f["name"] for f in build(detail(match=zero))["embeds"][0]["fields"]]

    assert "Why it matched" not in names


def test_oversized_text_stays_within_discord_limits() -> None:
    huge = listing_detail(title="T" * 5000)
    d = detail(property=state(address="A" * 5000), listings=[huge])

    embed = build(d)["embeds"][0]

    assert len(embed["title"]) == LIMITS["title"]
    assert embed["title"].endswith("…")
    assert all(len(f["value"]) <= LIMITS["field_value"] for f in embed["fields"])
    assert embed_length(embed) <= LIMITS["total"]


def test_escape_markdown() -> None:
    assert escape_markdown(r"*a* _b_ ~c~ `d` |e| > [f](g) # h \ i") == (
        r"\*a\* \_b\_ \~c\~ \`d\` \|e\| \> \[f\]\(g\) \# h \\ i"
    )


def test_truncate() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("abcdef  ghij", 9) == "abcdef…"
    assert len(truncate("x" * 300, "title")) == 256


def test_dashboard_url_joins_without_double_slashes() -> None:
    assert dashboard_url("https://furnest.example.com/", 42) == (
        "https://furnest.example.com/properties/42"
    )
    assert (
        dashboard_url("https://example.com/furnest", 1)
        == "https://example.com/furnest/properties/1"
    )


AP, DN, IQ, SZ = (
    DiscordAction.APPROVE,
    DiscordAction.DENY,
    DiscordAction.INQUIRE,
    DiscordAction.SNOOZE,
)


@pytest.mark.parametrize(
    ("status", "disabled"),
    [
        (PropertyStatus.NEW, {AP, IQ}),
        (PropertyStatus.SUGGESTED, set()),
        (PropertyStatus.APPROVED, {AP}),
        (PropertyStatus.DENIED, {DN, IQ}),
        (PropertyStatus.INQUIRY_DRAFTED, {AP, DN, IQ}),  # Approve would drop the draft
        (PropertyStatus.INQUIRY_SENT, {AP, DN, IQ}),
        (PropertyStatus.UNAVAILABLE, {AP, DN, IQ}),
        (PropertyStatus.ARCHIVED, {AP, DN, IQ, SZ}),
        (PropertyStatus.WON, {AP, DN, IQ, SZ}),
        (PropertyStatus.LOST, {AP, DN, IQ, SZ}),
    ],
)
def test_disabled_actions_follow_the_state_machine(
    status: PropertyStatus, disabled: set[DiscordAction]
) -> None:
    assert disabled_actions(state(status=status)) == disabled


def test_footer_shows_an_active_snooze() -> None:
    snoozed = detail(property=state(snoozed_until=datetime(2026, 10, 4, 9, 0, tzinfo=UTC)))

    footer = build(snoozed)["embeds"][0]["footer"]["text"]

    assert footer == "furnest · suggested · snoozed until 2026-10-04"
