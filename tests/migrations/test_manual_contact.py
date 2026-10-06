"""0014 lets an email have no From address (an inquiry drafted or sent by hand without
SMTP), backfills ``properties.manual_contact_required`` from the listings, and its
downgrade drops the emails without a From address so the column can be NOT NULL again."""

import json

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-06 12:00:00"


def _property(engine: Engine, property_id: int, contact: dict[str, object] | None) -> None:
    data: dict[str, object] = {"title": "Furnished place"}
    if contact is not None:
        data["contact"] = contact
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO properties (id, status, manual_contact_required,"
                " status_changed_at, created_at, updated_at) VALUES (:id, 'new', 0, :t, :t, :t)"
            ),
            {"id": property_id, "t": T},
        )
        connection.execute(
            text(
                "INSERT INTO listings (source, source_id, property_id, url, title, data,"
                " content_hash, first_seen, last_seen, missed_runs, created_at, updated_at)"
                " VALUES ('test', :sid, :id, 'https://example.com', 't', :data, 'h',"
                " :t, :t, 0, :t, :t)"
            ),
            {"sid": f"l{property_id}", "id": property_id, "data": json.dumps(data), "t": T},
        )


def _email(engine: Engine, message_id: str, from_address: str | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO emails (direction, to_address, from_address, subject,"
                " body_rendered, status, attempts, message_id, created_at)"
                " VALUES ('outbound', NULL, :from, 's', 'b', 'draft', 0, :mid, :t)"
            ),
            {"from": from_address, "mid": message_id, "t": T},
        )


def _flags(engine: Engine) -> dict[int, bool]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, manual_contact_required FROM properties"))
        return {row[0]: bool(row[1]) for row in rows}


def test_the_flag_is_backfilled_from_the_listings(engine: Engine) -> None:
    upgrade(engine, "0013")
    _property(engine, 1, {"email": "lister@example.com"})
    _property(engine, 2, {"email": None, "platform_only": True})
    _property(engine, 3, None)

    upgrade(engine)

    assert _flags(engine) == {1: False, 2: True, 3: True}


def test_an_email_without_a_from_address_is_rejected_before_0014(engine: Engine) -> None:
    upgrade(engine, "0013")

    with pytest.raises(IntegrityError, match="NOT NULL"):
        _email(engine, "<1@example.com>", None)


def test_downgrade_drops_emails_without_a_from_address(engine: Engine) -> None:
    upgrade(engine)
    _email(engine, "<1@furnest.invalid>", None)
    _email(engine, "<2@example.com>", "a@example.com")

    downgrade(engine, "0013")

    with engine.connect() as connection:
        left = connection.execute(text("SELECT message_id FROM emails")).scalars().all()
    assert left == ["<2@example.com>"]
    with pytest.raises(IntegrityError, match="NOT NULL"):
        _email(engine, "<3@example.com>", None)
