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
