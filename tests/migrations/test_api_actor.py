"""0009 lets audit events and notes record the ``api`` actor, and its downgrade
drops those rows so the narrower constraint can come back."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-06 12:00:00"


def _seed(engine: Engine, actor: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO properties (id, status, manual_contact_required,"
                " status_changed_at, created_at, updated_at) VALUES (1, 'new', 0, :t, :t, :t)"
            ),
            {"t": T},
        )
        connection.execute(
            text(
                "INSERT INTO audit_events (property_id, to_status, actor, created_at)"
                " VALUES (1, 'new', :actor, :t)"
            ),
            {"actor": actor, "t": T},
        )
        connection.execute(
            text(
                "INSERT INTO notes (property_id, body_md, actor, created_at)"
                " VALUES (1, 'hi', :actor, :t)"
            ),
            {"actor": actor, "t": T},
        )


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def test_api_actor_is_rejected_before_0009(engine: Engine) -> None:
    upgrade(engine, "0008")

    with pytest.raises(IntegrityError, match="ck_audit_events_audit_event_actor"):
        _seed(engine, "api")


def test_api_actor_is_accepted_after_0009(migrated_engine: Engine) -> None:
    _seed(migrated_engine, "api")

    assert _count(migrated_engine, "audit_events") == 1
    assert _count(migrated_engine, "notes") == 1


def test_downgrade_drops_api_rows_and_keeps_the_rest(engine: Engine) -> None:
    upgrade(engine)
    _seed(engine, "api")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notes (property_id, body_md, actor, created_at)"
                " VALUES (1, 'kept', 'dashboard', :t)"
            ),
            {"t": T},
        )

    downgrade(engine, "0008")

    assert _count(engine, "audit_events") == 0
    assert _count(engine, "notes") == 1
    with pytest.raises(IntegrityError, match="ck_notes_note_actor"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notes (property_id, body_md, actor, created_at)"
                " VALUES (1, 'no', 'api', :t)"
            ),
            {"t": T},
        )
