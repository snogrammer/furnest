"""Inquiry templates (``email_templates``) and the signature (the ``email_signature`` setting).

Templates are saved as written; checking that one renders is the editor's job
(:func:`furnest.email.templates.render` against the sample context). ``default``
can be edited but not deleted, since the inquiry flow falls back to it.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.models import EmailTemplate
from furnest.db.session import session_scope
from furnest.db.settings_store import SqlSettingsStore
from furnest.email.templates import DEFAULT_TEMPLATE_NAME

SIGNATURE_KEY = "email_signature"


class TemplateNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no email template named {name!r}")
        self.name = name


class ProtectedTemplateError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"the {name!r} template can be edited but not deleted")
        self.name = name


@dataclass(frozen=True)
class StoredTemplate:
    id: int
    name: str
    subject: str
    body: str
    updated_at: datetime


def _stored(row: EmailTemplate) -> StoredTemplate:
    return StoredTemplate(
        id=row.id, name=row.name, subject=row.subject, body=row.body, updated_at=row.updated_at
    )


class SqlEmailTemplateStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._settings = SqlSettingsStore(session_factory)

    def list(self) -> list[StoredTemplate]:
        """Every template, ``default`` first, then by name."""
        statement = select(EmailTemplate).order_by(
            EmailTemplate.name != DEFAULT_TEMPLATE_NAME, EmailTemplate.name
        )
        with session_scope(self._session_factory) as session:
            return [_stored(row) for row in session.scalars(statement)]

    def get(self, name: str) -> StoredTemplate | None:
        with session_scope(self._session_factory) as session:
            row = session.scalar(select(EmailTemplate).where(EmailTemplate.name == name))
            return None if row is None else _stored(row)

    def save(self, name: str, *, subject: str, body: str) -> StoredTemplate:
        """Create the template, or replace its subject and body."""
        with session_scope(self._session_factory) as session:
            row = session.scalar(select(EmailTemplate).where(EmailTemplate.name == name))
            if row is None:
                row = EmailTemplate(name=name)
                session.add(row)
            row.subject = subject
            row.body = body
            row.updated_at = utcnow()
            session.flush()
            return _stored(row)

    def delete(self, name: str) -> None:
        if name == DEFAULT_TEMPLATE_NAME:
            raise ProtectedTemplateError(name)
        with session_scope(self._session_factory) as session:
            row = session.scalar(select(EmailTemplate).where(EmailTemplate.name == name))
            if row is None:
                raise TemplateNotFoundError(name)
            session.delete(row)

    def signature(self) -> str:
        """The signature, or ``""`` when none is set."""
        value = self._settings.get(SIGNATURE_KEY)
        return value if isinstance(value, str) else ""

    def set_signature(self, signature: str) -> None:
        self._settings.set(SIGNATURE_KEY, signature)
