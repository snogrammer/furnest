"""The migrations and the ORM models describe the same schema."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine

import furnest.db.models  # noqa: F401  (registers every table)
from furnest.db.base import Base

pytestmark = pytest.mark.migrations


def test_autogenerate_against_models_is_empty(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "render_as_batch": True}
        )
        diff = compare_metadata(context, Base.metadata)

    assert diff == []
