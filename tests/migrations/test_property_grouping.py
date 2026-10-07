"""0017 adds grouping columns and backfills ``listings.address_key`` from the property
address (street addresses only) and ``properties.match_listing_id``."""

import pytest
from sqlalchemy import Engine, text

from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations

T = "2026-10-07 12:00:00"


def _property(engine: Engine, property_id: int, address: str | None, *, matched: bool) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO properties (id, status, address, match_result,"
                " manual_contact_required, status_changed_at, created_at, updated_at)"
                " VALUES (:id, 'new', :address, :match, 0, :t, :t, :t)"
            ),
            {
                "id": property_id,
                "address": address,
                "match": '{"passed": true}' if matched else None,
                "t": T,
            },
        )
        connection.execute(
            text(
                "INSERT INTO listings (id, source, source_id, property_id, url, title, data,"
                " content_hash, first_seen, last_seen, missed_runs, created_at, updated_at)"
                " VALUES (:lid, 'test', :sid, :id, 'https://example.com', 't', '{}', 'h',"
                " :t, :t, 0, :t, :t)"
            ),
            {"lid": property_id * 10, "sid": f"l{property_id}", "id": property_id, "t": T},
        )


def test_address_keys_and_match_listing_are_backfilled(engine: Engine) -> None:
    upgrade(engine, "0016")
    _property(engine, 1, "123 n main st #4b, springfield, il 62701, us", matched=True)
    _property(engine, 2, "springfield, il, us", matched=False)
    _property(engine, 3, "po box 12, springfield, il, us", matched=True)
    _property(engine, 4, None, matched=False)

    upgrade(engine)

    with engine.connect() as connection:
        keys: dict[int, str | None] = dict(
            connection.execute(text("SELECT property_id, address_key FROM listings")).all()
        )
        matched: dict[int, int | None] = dict(
            connection.execute(text("SELECT id, match_listing_id FROM properties")).all()
        )
        merged = connection.execute(text("SELECT merged_into_id FROM properties")).scalars().all()
    assert keys == {
        1: "123 n main st #4b, springfield, il 62701, us",
        2: None,
        3: "po box 12, springfield, il, us",
        4: None,
    }
    assert matched == {1: 10, 2: None, 3: 30, 4: None}
    assert merged == [None, None, None, None]


def test_downgrade_drops_the_columns(engine: Engine) -> None:
    upgrade(engine)
    downgrade(engine, "0016")

    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(properties)"))}
    assert "merged_into_id" not in columns
    assert "match_listing_id" not in columns
