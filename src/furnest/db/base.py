"""Declarative base, constraint naming convention and UTC timestamps."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Dialect, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

# Deterministic constraint names, so Alembic batch migrations on SQLite (which
# rebuild tables) can find and alter constraints by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on top of SQLite's tz-less storage.

    Values are stored as naive UTC and come back with ``tzinfo=UTC``. Naive
    datetimes are rejected on write, so local times can't slip in by accident.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "naive datetime; use an aware datetime (e.g. furnest.db.base.utcnow())"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: object, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"expected a datetime from the database, got {type(value).__name__}"
            raise TypeError(msg)
        return value.replace(tzinfo=UTC)
