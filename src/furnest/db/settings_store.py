"""SQL-backed :class:`furnest.config.SettingsStore` over the ``settings`` table."""

from pydantic import JsonValue
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.models import Setting
from furnest.db.session import session_scope


class SqlSettingsStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str) -> JsonValue | None:
        with session_scope(self._session_factory) as session:
            row = session.get(Setting, key)
            return None if row is None else row.value

    def set(self, key: str, value: JsonValue) -> None:
        with session_scope(self._session_factory) as session:
            session.merge(Setting(key=key, value=value, updated_at=utcnow()))

    def delete(self, key: str) -> None:
        with session_scope(self._session_factory) as session:
            session.execute(delete(Setting).where(Setting.key == key))
