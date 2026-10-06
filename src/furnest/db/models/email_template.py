"""Inquiry templates; rendering is in ``furnest.email.templates``.

One row per named template, edited in place from the dashboard. Migration 0012
seeds ``default``, which the inquiry flow uses and the editor won't delete. The
signature isn't here: it's the ``email_signature`` setting, shared by every template.
"""

from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Jinja2 source, rendered in a sandbox.
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
