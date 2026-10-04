"""SqlSourceStore on a migrated database."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.source_store import (
    MAX_ERROR_LENGTH,
    SourceNotFoundError,
    SourceState,
    SqlSourceStore,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlSourceStore:
    store = SqlSourceStore(session_factory)
    store.ensure(["beta", "alpha"])
    return store


def test_ensure_creates_blank_rows(store: SqlSourceStore) -> None:
    assert store.all() == [
        SourceState("alpha", None, None, None, 0, None, 0),
        SourceState("beta", None, None, None, 0, None, 0),
    ]


def test_ensure_is_idempotent_and_keeps_existing_state(store: SqlSourceStore) -> None:
    store.set_enabled("alpha", False)

    store.ensure(["alpha", "gamma"])
    store.ensure([])

    assert [s.name for s in store.all()] == ["alpha", "beta", "gamma"]
    state = store.get("alpha")
    assert state is not None
    assert state.enabled is False


def test_get_missing_returns_none(store: SqlSourceStore) -> None:
    assert store.get("missing") is None


def test_set_enabled_sets_and_clears(store: SqlSourceStore) -> None:
    store.set_enabled("alpha", True)
    store.set_enabled("beta", False)
    assert store.stored_enabled() == {"alpha": True, "beta": False}

    store.set_enabled("alpha", None)
    assert store.stored_enabled() == {"alpha": None, "beta": False}


def test_unknown_source_raises(store: SqlSourceStore) -> None:
    with pytest.raises(SourceNotFoundError, match="missing"):
        store.set_enabled("missing", True)
    with pytest.raises(SourceNotFoundError):
        store.record_failure("missing", error="boom", at=T0)


def test_failures_count_up_and_success_resets(store: SqlSourceStore) -> None:
    store.record_failure("alpha", error="blocked", at=T0)
    store.record_failure("alpha", error="timeout", at=T0 + timedelta(hours=1))

    state = store.get("alpha")
    assert state is not None
    assert state.consecutive_failures == 2
    assert state.last_error == "timeout"
    assert state.last_run_at == T0 + timedelta(hours=1)
    assert state.last_success_at is None

    store.record_success("alpha", listings_found=7, at=T0 + timedelta(hours=2))

    state = store.get("alpha")
    assert state is not None
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.listings_found == 7
    assert state.last_run_at == state.last_success_at == T0 + timedelta(hours=2)
    # The other source is untouched.
    assert store.get("beta") == SourceState("beta", None, None, None, 0, None, 0)


def test_long_errors_are_truncated(store: SqlSourceStore) -> None:
    store.record_failure("alpha", error="x" * (MAX_ERROR_LENGTH + 500), at=T0)

    state = store.get("alpha")
    assert state is not None
    assert state.last_error == "x" * MAX_ERROR_LENGTH


def test_set_disabled_reasons_sets_and_clears(store: SqlSourceStore) -> None:
    store.set_disabled_reasons({"alpha": "needs a browser: Playwright is not installed"})

    alpha = store.get("alpha")
    assert alpha is not None
    assert alpha.disabled_reason == "needs a browser: Playwright is not installed"
    assert alpha.enabled is None
    assert alpha.consecutive_failures == 0

    store.set_disabled_reasons({"alpha": None, "beta": None})

    assert [s.disabled_reason for s in store.all()] == [None, None]


def test_set_disabled_reasons_unknown_source_raises(store: SqlSourceStore) -> None:
    with pytest.raises(SourceNotFoundError):
        store.set_disabled_reasons({"missing": "reason"})
