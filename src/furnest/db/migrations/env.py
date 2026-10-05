"""Alembic environment.

The connection is resolved in this order:

1. ``config.attributes["connection"]``: an open connection handed in by
   :mod:`furnest.db.migrate` (the CLI, tests and web auto-migrate use this);
2. ``sqlalchemy.url`` in the Alembic config;
3. ``furnest.config.get_settings().database_url``, which is what a bare
   ``uv run alembic ...`` uses.

``render_as_batch`` is required on SQLite, which can't ALTER most things in place.
Alembic rebuilds the table instead. Migrations therefore run with foreign keys off
and a ``foreign_key_check`` before commit (see :func:`furnest.db.engine.foreign_keys_off`),
so a connection handed in must not have begun a transaction yet.
"""

from alembic import context
from sqlalchemy import Connection

import furnest.db.models  # noqa: F401  (registers every table on Base.metadata)
from furnest.config import get_settings
from furnest.db.base import Base
from furnest.db.engine import check_foreign_keys, create_sqlite_engine, foreign_keys_off

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        # SQLite does run DDL inside transactions, though Alembic's SQLite impl
        # says otherwise. Saying so makes the whole upgrade one transaction, so the
        # foreign key check below runs before anything commits.
        transactional_ddl=True,
    )
    with foreign_keys_off(connection), context.begin_transaction():
        context.run_migrations()
        check_foreign_keys(connection)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_sqlite_engine(_url(), busy_timeout_ms=get_settings().db.busy_timeout_ms)
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
