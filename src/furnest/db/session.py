"""Sessions and the unit-of-work helper.

Use one short ``session_scope`` per unit of work. It commits on success, rolls back
on any exception, and always closes. Every transaction holds the SQLite write lock
(see :mod:`furnest.db.engine`), so keep scopes short and never wait on network I/O
inside one.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False so objects stay readable after the scope closes.
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
