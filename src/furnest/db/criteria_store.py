"""CriteriaStore: the versioned search profile over the ``criteria_versions`` table.

Every save appends version ``max + 1``. With ``activate=True`` (the default) the new
version becomes the active one in the same transaction, so readers never see zero
or two active versions. :meth:`CriteriaStore.activate` rolls back to (or forward to)
any stored version without copying it. Rows are never updated except for
``is_active`` and never deleted.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import CriteriaVersion
from furnest.db.session import session_scope
from furnest.domain.criteria import Criteria, CriteriaChange, diff_criteria


class CriteriaVersionNotFoundError(LookupError):
    def __init__(self, version: int) -> None:
        super().__init__(f"criteria version {version} does not exist")
        self.version = version


@dataclass(frozen=True, slots=True)
class CriteriaRecord:
    version: int
    criteria: Criteria
    created_at: datetime
    created_by: str
    is_active: bool


def _record(row: CriteriaVersion) -> CriteriaRecord:
    return CriteriaRecord(
        version=row.version,
        criteria=Criteria.model_validate(row.data),
        created_at=row.created_at,
        created_by=row.created_by,
        is_active=row.is_active,
    )


def _deactivate_all(session: Session) -> None:
    session.execute(
        update(CriteriaVersion).where(CriteriaVersion.is_active).values(is_active=False)
    )
    # Flush the clear before setting the new flag, so the one-active index never sees two.
    session.flush()


class CriteriaStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def active(self) -> CriteriaRecord | None:
        """The active version, or ``None`` before any criteria have been saved."""
        with session_scope(self._session_factory) as session:
            row = session.scalar(select(CriteriaVersion).where(CriteriaVersion.is_active))
            return None if row is None else _record(row)

    def get(self, version: int) -> CriteriaRecord:
        with session_scope(self._session_factory) as session:
            return _record(self._row(session, version))

    def history(self) -> list[CriteriaRecord]:
        """All versions, newest first."""
        with session_scope(self._session_factory) as session:
            rows = session.scalars(select(CriteriaVersion).order_by(CriteriaVersion.version.desc()))
            return [_record(row) for row in rows]

    def save(self, criteria: Criteria, *, created_by: str, activate: bool = True) -> CriteriaRecord:
        """Store ``criteria`` as a new version, active unless ``activate=False``.

        The first version ever saved is always activated, so a non-empty history
        always has exactly one active version.
        """
        with session_scope(self._session_factory) as session:
            latest = session.scalar(select(func.max(CriteriaVersion.version)))
            activate = activate or latest is None
            if activate:
                _deactivate_all(session)
            row = CriteriaVersion(
                version=(latest or 0) + 1,
                data=criteria.model_dump(mode="json"),
                created_by=created_by,
                is_active=activate,
            )
            session.add(row)
            session.flush()
            return _record(row)

    def activate(self, version: int) -> CriteriaRecord:
        """Make an existing version the active one."""
        with session_scope(self._session_factory) as session:
            row = self._row(session, version)
            if not row.is_active:
                _deactivate_all(session)
                row.is_active = True
                session.flush()
            return _record(row)

    def diff(self, old_version: int, new_version: int) -> list[CriteriaChange]:
        """Field-level changes from ``old_version`` to ``new_version``."""
        return diff_criteria(self.get(old_version).criteria, self.get(new_version).criteria)

    @staticmethod
    def _row(session: Session, version: int) -> CriteriaVersion:
        row = session.scalar(select(CriteriaVersion).where(CriteriaVersion.version == version))
        if row is None:
            raise CriteriaVersionNotFoundError(version)
        return row
