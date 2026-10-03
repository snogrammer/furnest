"""SQLite engine factory.

Web and worker share one SQLite file, so every connection is set up for
concurrent use:

* ``journal_mode=WAL`` lets readers proceed while one writer commits.
* ``busy_timeout`` makes a connection wait for a lock instead of failing at once.
* ``foreign_keys=ON`` because SQLite leaves them off by default.
* ``synchronous=NORMAL`` is durable enough under WAL and much faster than FULL.

Every transaction starts with ``BEGIN IMMEDIATE``. Under the default deferred
``BEGIN``, a transaction that reads and then writes can find that another process
committed in between. SQLite then returns ``database is locked`` straight away,
without waiting on ``busy_timeout``. Taking the write lock up front turns that into
an ordinary wait. Readers in other processes are never blocked under WAL. The setup
follows SQLAlchemy's documented pysqlite recipe: the driver's own transaction
handling is turned off and SQLAlchemy emits ``BEGIN`` itself.
"""

import sqlite3
from functools import partial
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import ConnectionPoolEntry

from furnest.config import Settings


def create_engine_from_settings(settings: Settings) -> Engine:
    return create_sqlite_engine(settings.database_url, busy_timeout_ms=settings.db.busy_timeout_ms)


def create_sqlite_engine(url: str, *, busy_timeout_ms: int = 5000) -> Engine:
    sa_url = make_url(url)
    if sa_url.get_backend_name() != "sqlite":
        msg = f"only SQLite is supported, got {sa_url.get_backend_name()!r}"
        raise ValueError(msg)
    if sa_url.database and sa_url.database != ":memory:":
        Path(sa_url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(sa_url)
    event.listen(engine, "connect", partial(_configure_connection, busy_timeout_ms))
    event.listen(engine, "begin", _begin_immediate)
    return engine


def _configure_connection(
    busy_timeout_ms: int, dbapi_connection: sqlite3.Connection, _record: ConnectionPoolEntry
) -> None:
    # Hand transaction control to SQLAlchemy (see _begin_immediate).
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _begin_immediate(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")
