"""SqlEmailTemplateStore against a migrated database: the seeded default, edits, the signature."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.email_template_store import (
    ProtectedTemplateError,
    SqlEmailTemplateStore,
    TemplateNotFoundError,
)
from furnest.email.templates import DEFAULT_BODY, DEFAULT_SUBJECT, DEFAULT_TEMPLATE_NAME

pytestmark = pytest.mark.integration


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlEmailTemplateStore:
    return SqlEmailTemplateStore(session_factory)


def test_migration_seeds_the_default(store: SqlEmailTemplateStore) -> None:
    default = store.get(DEFAULT_TEMPLATE_NAME)

    assert default is not None
    assert (default.subject, default.body) == (DEFAULT_SUBJECT, DEFAULT_BODY)
    assert default.updated_at.tzinfo is not None
    assert [t.name for t in store.list()] == [DEFAULT_TEMPLATE_NAME]


def test_save_creates_then_updates(store: SqlEmailTemplateStore) -> None:
    created = store.save("short", subject="Hi", body="One")
    updated = store.save("short", subject="Hello", body="Two")

    assert updated.id == created.id
    assert updated.updated_at >= created.updated_at
    stored = store.get("short")
    assert stored is not None
    assert (stored.subject, stored.body) == ("Hello", "Two")


def test_list_puts_default_first(store: SqlEmailTemplateStore) -> None:
    store.save("zeta", subject="s", body="b")
    store.save("alpha", subject="s", body="b")

    assert [t.name for t in store.list()] == [DEFAULT_TEMPLATE_NAME, "alpha", "zeta"]


def test_delete(store: SqlEmailTemplateStore) -> None:
    store.save("gone", subject="s", body="b")

    store.delete("gone")

    assert store.get("gone") is None
    with pytest.raises(TemplateNotFoundError):
        store.delete("gone")


def test_default_cannot_be_deleted(store: SqlEmailTemplateStore) -> None:
    with pytest.raises(ProtectedTemplateError):
        store.delete(DEFAULT_TEMPLATE_NAME)
    assert store.get(DEFAULT_TEMPLATE_NAME) is not None


def test_signature_round_trip(store: SqlEmailTemplateStore) -> None:
    assert store.signature() == ""

    store.set_signature("A. Person\nexample@example.com")

    assert store.signature() == "A. Person\nexample@example.com"
