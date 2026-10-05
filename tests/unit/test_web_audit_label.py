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
