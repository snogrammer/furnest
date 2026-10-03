"""SqlSettingsStore against a migrated database, through RuntimeSettings."""

import pytest
from sqlalchemy import Engine

from furnest.config import INQUIRY_MODE, InquiryMode, RuntimeSettings, Settings
from furnest.db.session import make_session_factory
from furnest.db.settings_store import SqlSettingsStore


@pytest.fixture
def store(migrated_engine: Engine) -> SqlSettingsStore:
    return SqlSettingsStore(make_session_factory(migrated_engine))


def test_round_trip(store: SqlSettingsStore) -> None:
    assert store.get("missing") is None

    store.set("k", {"nested": [1, 2, "three"]})
    assert store.get("k") == {"nested": [1, 2, "three"]}

    store.set("k", "replaced")
    assert store.get("k") == "replaced"

    store.delete("k")
    assert store.get("k") is None
    store.delete("k")  # deleting a missing key is a no-op


def test_precedence_through_runtime_settings(
    store: SqlSettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeSettings(Settings(), store)
    assert runtime.get(INQUIRY_MODE) is InquiryMode.REVIEW

    runtime.set(INQUIRY_MODE, InquiryMode.AUTO)
    assert runtime.get(INQUIRY_MODE) is InquiryMode.AUTO

    monkeypatch.setenv("INQUIRY_MODE", "review")
    pinned = RuntimeSettings(Settings(), store)
    assert pinned.get(INQUIRY_MODE) is InquiryMode.REVIEW
