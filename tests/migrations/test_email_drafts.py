"""0013 lets ``emails`` hold drafts (status ``draft``, maybe no recipient yet), and
its downgrade drops them so the narrower constraints can come back."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-06 12:00:00"


def _insert(engine: Engine, message_id: str, status: str, to: str | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO emails (direction, to_address, from_address, subject,"
                " body_rendered, status, attempts, message_id, created_at)"
                " VALUES ('outbound', :to, 'a@example.com', 's', 'b', :status, 0, :mid, :t)"
            ),
            {"to": to, "status": status, "mid": message_id, "t": T},
        )


def _count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM emails")).scalar_one())


def test_drafts_are_rejected_before_0013(engine: Engine) -> None:
    upgrade(engine, "0012")

    with pytest.raises(IntegrityError, match="ck_emails_email_status"):
        _insert(engine, "<1@example.com>", "draft", "b@example.com")
    with pytest.raises(IntegrityError, match="NOT NULL"):
        _insert(engine, "<2@example.com>", "queued", None)


def test_a_draft_without_a_recipient_is_accepted_after_0013(migrated_engine: Engine) -> None:
    _insert(migrated_engine, "<1@example.com>", "draft", None)

    assert _count(migrated_engine) == 1


def test_downgrade_drops_drafts_and_keeps_the_rest(engine: Engine) -> None:
    upgrade(engine)
    _insert(engine, "<1@example.com>", "draft", None)
    _insert(engine, "<2@example.com>", "draft", "b@example.com")
    _insert(engine, "<3@example.com>", "sent", "b@example.com")

    downgrade(engine, "0012")

    assert _count(engine) == 1
    with pytest.raises(IntegrityError, match="ck_emails_email_status"):
        _insert(engine, "<4@example.com>", "draft", "b@example.com")
