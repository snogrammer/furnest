"""audit_label: one readable line per audit event on the detail page's timeline."""

from datetime import UTC, datetime
from typing import Any

import pytest

from furnest.db.property_store import AuditEntry
from furnest.domain import Actor, PropertyStatus
from furnest.web.property_views import audit_label

S = PropertyStatus


def entry(
    from_status: PropertyStatus | None, to_status: PropertyStatus, payload: dict[str, Any] | None
) -> AuditEntry:
    return AuditEntry(
        id=1,
        property_id=1,
        from_status=from_status,
        to_status=to_status,
        actor=Actor.SYSTEM,
        actor_detail=None,
        payload=payload,
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("event", "label"),
    [
        (entry(None, S.NEW, {"listing_id": 1}), "Created as new"),
        (entry(S.SUGGESTED, S.INQUIRY_SENT, None), "suggested → inquiry sent"),
        (
            entry(S.SUGGESTED, S.SUGGESTED, {"snoozed_until": "2026-10-12T00:00:00+00:00"}),
            "Snoozed until 2026-10-12",
        ),
        (entry(S.SUGGESTED, S.SUGGESTED, {"unsnoozed": True}), "Snooze ended"),
        (entry(S.APPROVED, S.APPROVED, None), "Updated while approved"),
    ],
)
def test_audit_label(event: AuditEntry, label: str) -> None:
    assert audit_label(event) == label


def test_grouping_events_are_labelled() -> None:
    def event(detail: str, payload: dict[str, Any], property_id: int = 1) -> AuditEntry:
        return AuditEntry(
            id=1,
            property_id=property_id,
            from_status=S.SUGGESTED,
            to_status=S.SUGGESTED,
            actor=Actor.SYSTEM,
            actor_detail=detail,
            payload=payload,
            created_at=datetime(2026, 10, 5, tzinfo=UTC),
        )

    assert audit_label(event("grouped", {"listing_id": 7})) == "Listing #7 grouped here"
    assert audit_label(event("merged", {"merged_into": 3})) == "Merged into #3"
    assert (
        audit_label(event("merged", {"merged_from": 2, "merged_status": "approved"}))
        == "Merged #2 into this property (was approved)"
    )
    assert audit_label(event("merged", {"merged_from": 2})) == "Merged #2 into this property"
    # On the survivor's timeline, the merged property's events say whose they are.
    assert audit_label(event("grouped", {"listing_id": 7}, 2), 1) == "#2: Listing #7 grouped here"
    assert audit_label(event("grouped", {"listing_id": 7}, 1), 1) == "Listing #7 grouped here"
    assert (
        audit_label(event("split", {"listing_id": 7, "split_into": 9}))
        == "Listing #7 split off into #9"
    )
    assert (
        audit_label(entry(None, S.NEW, {"listing_id": 7, "split_from": 1}))
        == "Created as new (split from #1)"
    )


def relisting(
    payload: dict[str, Any], from_status: PropertyStatus = S.DENIED, to: PropertyStatus = S.DENIED
) -> AuditEntry:
    return AuditEntry(
        id=1,
        property_id=1,
        from_status=from_status,
        to_status=to,
        actor=Actor.SYSTEM,
        actor_detail="relisted",
        payload=payload,
        created_at=datetime(2026, 10, 15, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (
            {
                "listing_id": 7,
                "gone_since": "2026-10-05T00:00:00+00:00",
                "price_before_minor": 240000,
                "price_now_minor": 210000,
                "currency": "USD",
            },
            "Relisted (listing #7) after 10 days · $2,400.00 → $2,100.00/mo",
        ),
        (
            {
                "listing_id": 7,
                "price_before_minor": 240000,
                "price_now_minor": 240000,
                "currency": "USD",
            },
            "Relisted (listing #7) · $2,400.00/mo",
        ),
        (
            {"listing_id": 7, "gone_since": "2026-10-14T00:00:00+00:00"},
            "Relisted (listing #7) after 1 day",
        ),
        ({"gone_since": "not a date", "price_now_minor": "x", "currency": "USD"}, "Relisted"),
        ({}, "Relisted"),
        ({"price_now_minor": 100, "currency": "ZZZ"}, "Relisted"),
    ],
)
def test_relisting_is_labelled(payload: dict[str, Any], label: str) -> None:
    assert audit_label(relisting(payload)) == label


def test_a_relisting_out_of_unavailable_shows_the_move() -> None:
    event = relisting({"listing_id": 7}, S.UNAVAILABLE, S.APPROVED)

    assert audit_label(event) == "unavailable → approved: Relisted (listing #7)"


def test_all_listings_removed_is_labelled() -> None:
    event = AuditEntry(
        id=1,
        property_id=1,
        from_status=S.DENIED,
        to_status=S.DENIED,
        actor=Actor.SYSTEM,
        actor_detail="listings removed",
        payload={},
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    assert audit_label(event) == "All listings removed"
