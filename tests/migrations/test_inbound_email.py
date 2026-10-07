"""0015 lets ``emails`` log inbound replies (status ``received``), and its
downgrade drops them so the narrower constraint can come back."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-06 12:00:00"


def _insert(engine: Engine, message_id: str, direction: str, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO emails (direction, to_address, from_address, subject,"
                " body_rendered, status, attempts, message_id, created_at)"
                " VALUES (:direction, 'a@example.com', 'b@example.com', 's', 'b',"
                " :status, 0, :mid, :t)"
            ),
            {"direction": direction, "status": status, "mid": message_id, "t": T},
        )


def _count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM emails")).scalar_one())


def test_received_is_rejected_before_0015(engine: Engine) -> None:
    upgrade(engine, "0014")

    with pytest.raises(IntegrityError, match="ck_emails_email_status"):
        _insert(engine, "<1@example.com>", "inbound", "received")


def test_a_received_reply_is_accepted_after_0015(migrated_engine: Engine) -> None:
    _insert(migrated_engine, "<1@example.com>", "inbound", "received")

    assert _count(migrated_engine) == 1


def test_downgrade_drops_replies_and_keeps_the_rest(engine: Engine) -> None:
    upgrade(engine)
    _insert(engine, "<1@example.com>", "inbound", "received")
    _insert(engine, "<2@example.com>", "outbound", "sent")

    downgrade(engine, "0014")

    assert _count(engine) == 1
    with pytest.raises(IntegrityError, match="ck_emails_email_status"):
        _insert(engine, "<3@example.com>", "inbound", "received")
