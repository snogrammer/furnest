"""0022 adds ``properties.owner_type`` (backfilled to ``unknown``) and the nullable
``owner_type_override``, both limited to the owner types by CHECK constraints."""

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-08 12:00:00"


def _property(engine: Engine, property_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO properties (id, status, manual_contact_required,"
                " status_changed_at, created_at, updated_at) VALUES (:id, 'new', 0, :t, :t, :t)"
            ),
            {"id": property_id, "t": T},
        )


def _owner_types(engine: Engine) -> dict[int, tuple[str, str | None]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, owner_type, owner_type_override FROM properties")
        )
        return {row[0]: (row[1], row[2]) for row in rows}


def _columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("properties")}


def test_existing_properties_start_unknown(engine: Engine) -> None:
    upgrade(engine, "0021")
    _property(engine, 1)

    upgrade(engine)
    _property(engine, 2)

    assert _owner_types(engine) == {1: ("unknown", None), 2: ("unknown", None)}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE properties SET owner_type = :kind WHERE id = 1",
        "UPDATE properties SET owner_type_override = :kind WHERE id = 1",
    ],
)
def test_only_owner_types_are_accepted(engine: Engine, statement: str) -> None:
    upgrade(engine)
    _property(engine, 1)

    with engine.begin() as connection:
        connection.execute(text(statement), {"kind": "agent"})
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(text(statement), {"kind": "landlord"})


def test_downgrade_removes_the_columns(engine: Engine) -> None:
    upgrade(engine)
    _property(engine, 1)
    assert {"owner_type", "owner_type_override"} <= _columns(engine)

    downgrade(engine, "0021")

    assert not {"owner_type", "owner_type_override"} & _columns(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM properties")).scalar() == 1
