"""0023 adds the nullable ``sources.overrides`` JSON column; existing rows keep going."""

import json

import pytest
from sqlalchemy import Engine, inspect, text

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-09 12:00:00"


def _source(engine: Engine, name: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources (name, consecutive_failures, listings_found,"
                " created_at, updated_at) VALUES (:name, 0, 0, :t, :t)"
            ),
            {"name": name, "t": T},
        )


def _columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("sources")}


def test_existing_sources_get_no_overrides(engine: Engine) -> None:
    upgrade(engine, "0022")
    _source(engine, "alpha")
    assert "overrides" not in _columns(engine)

    upgrade(engine)
    _source(engine, "beta")

    assert "overrides" in _columns(engine)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE sources SET overrides = :value WHERE name = 'beta'"),
            {"value": json.dumps({"max_results": 5})},
        )
        rows = connection.execute(text("SELECT name, overrides FROM sources ORDER BY name"))
        assert [(row[0], row[1]) for row in rows] == [
            ("alpha", None),
            ("beta", '{"max_results": 5}'),
        ]


def test_downgrade_drops_the_column_and_keeps_the_rows(engine: Engine) -> None:
    upgrade(engine)
    _source(engine, "alpha")

    downgrade(engine, "0022")

    assert "overrides" not in _columns(engine)
    with engine.connect() as connection:
        names = list(connection.execute(text("SELECT name FROM sources")).scalars())
    assert names == ["alpha"]
