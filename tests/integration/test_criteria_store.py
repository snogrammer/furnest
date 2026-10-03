"""CriteriaStore: append-only versions with exactly one active version."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.criteria_store import CriteriaStore, CriteriaVersionNotFoundError
from furnest.db.models import CriteriaVersion
from furnest.db.session import session_scope
from furnest.domain import Criteria, CriteriaChange


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> CriteriaStore:
    return CriteriaStore(session_factory)


def active_count(factory: sessionmaker[Session]) -> int:
    with session_scope(factory) as session:
        count = session.scalar(
            select(func.count()).select_from(CriteriaVersion).where(CriteriaVersion.is_active)
        )
        return count or 0


def test_empty_store(store: CriteriaStore) -> None:
    assert store.active() is None
    assert store.history() == []
    with pytest.raises(CriteriaVersionNotFoundError, match="version 1"):
        store.get(1)


def test_save_appends_versions_and_activates(
    store: CriteriaStore, session_factory: sessionmaker[Session]
) -> None:
    first = store.save(Criteria(min_beds=1), created_by="dashboard")
    assert (first.version, first.is_active, first.created_by) == (1, True, "dashboard")
    assert first.created_at.tzinfo is not None

    second = store.save(Criteria(min_beds=2), created_by="discord")
    assert second.version == 2
    active = store.active()
    assert active is not None
    assert active.version == 2
    assert active.criteria == Criteria(min_beds=2)
    assert store.get(1).is_active is False
    assert [r.version for r in store.history()] == [2, 1]
    assert active_count(session_factory) == 1


def test_save_without_activating(store: CriteriaStore) -> None:
    store.save(Criteria(min_beds=1), created_by="dashboard")
    draft = store.save(Criteria(min_beds=3), created_by="dashboard", activate=False)

    assert draft.is_active is False
    active = store.active()
    assert active is not None
    assert active.version == 1


def test_first_version_is_always_active(store: CriteriaStore) -> None:
    first = store.save(Criteria(), created_by="system", activate=False)
    assert first.is_active is True


def test_activate_old_version(store: CriteriaStore, session_factory: sessionmaker[Session]) -> None:
    store.save(Criteria(min_beds=1), created_by="dashboard")
    store.save(Criteria(min_beds=2), created_by="dashboard")

    rolled_back = store.activate(1)
    assert rolled_back.is_active is True
    active = store.active()
    assert active is not None
    assert active.criteria == Criteria(min_beds=1)
    assert active_count(session_factory) == 1
    # History is untouched: no copy was made.
    assert [r.version for r in store.history()] == [2, 1]

    assert store.activate(1).is_active is True  # activating the active version is a no-op
    assert active_count(session_factory) == 1


def test_activate_missing_version(store: CriteriaStore) -> None:
    store.save(Criteria(), created_by="dashboard")
    with pytest.raises(CriteriaVersionNotFoundError):
        store.activate(9)
    active = store.active()
    assert active is not None
    assert active.version == 1


def test_diff_between_versions(store: CriteriaStore) -> None:
    store.save(Criteria(min_beds=1), created_by="dashboard")
    store.save(Criteria(min_beds=2, keywords_deny=("basement",)), created_by="dashboard")

    assert store.diff(1, 2) == [
        CriteriaChange(field="min_beds", old=1, new=2),
        CriteriaChange(field="keywords_deny", old=[], new=["basement"]),
    ]
    with pytest.raises(CriteriaVersionNotFoundError):
        store.diff(1, 3)


def test_database_rejects_two_active_versions(
    store: CriteriaStore, session_factory: sessionmaker[Session]
) -> None:
    store.save(Criteria(), created_by="dashboard")
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        session.add(CriteriaVersion(version=2, data={}, created_by="test", is_active=True))


def test_database_rejects_duplicate_version_numbers(
    store: CriteriaStore, session_factory: sessionmaker[Session]
) -> None:
    store.save(Criteria(), created_by="dashboard")
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        session.add(CriteriaVersion(version=1, data={}, created_by="test", is_active=False))


def test_versions_survive_a_new_store_instance(
    store: CriteriaStore, session_factory: sessionmaker[Session]
) -> None:
    store.save(Criteria(neighborhoods=("old town",)), created_by="dashboard")
    reopened = CriteriaStore(session_factory)
    active = reopened.active()
    assert active is not None
    assert active.criteria.neighborhoods == ("old town",)
