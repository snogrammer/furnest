"""Properties and their audit trail; the logic is in ``furnest.db.property_store``.

A property is the thing under review: one real unit, which one or more listings
(``listings.property_id``) advertise. Until listings are grouped (P7) every
property has exactly one listing. ``status`` is its place in the pipeline
(:class:`~furnest.domain.PropertyStatus`) and only changes through
``SqlPropertyStore.transition``, which writes an ``audit_events`` row in the same
transaction. The first event of every property has ``from_status`` NULL: its
creation.

``score``, ``match_result`` and ``criteria_version_id`` hold the latest match, and
``snoozed_until``, ``manual_contact_required`` and the Discord message reference
are set by the pipeline and the Discord integration.

A :class:`Note` is free-form markdown someone wrote about a property. Deny reasons
are notes too; the deny's audit event names its note in ``payload["note_id"]``.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow
from furnest.domain import Actor, PropertyStatus


def _actor_enum(name: str) -> Enum:
    return Enum(
        Actor,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda actors: [a.value for a in actors],
    )


def _status_enum(name: str) -> Enum:
    return Enum(
        PropertyStatus,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda statuses: [s.value for s in statuses],
    )


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[PropertyStatus] = mapped_column(
        _status_enum("property_status"), default=PropertyStatus.NEW, index=True
    )
    # ParsedAddress.canonical of the listing's address, or NULL if it gave none.
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    score: Mapped[int | None]
    # The MatchResult as JSON.
    match_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    criteria_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("criteria_versions.id", ondelete="SET NULL")
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    manual_contact_required: Mapped[bool] = mapped_column(default=False)
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    discord_message_id: Mapped[str | None] = mapped_column(String(32))
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_property_id_created_at", "property_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"))
    # NULL on the event that created the property.
    from_status: Mapped[PropertyStatus | None] = mapped_column(
        _status_enum("audit_event_from_status")
    )
    to_status: Mapped[PropertyStatus] = mapped_column(_status_enum("audit_event_to_status"))
    actor: Mapped[Actor] = mapped_column(_actor_enum("audit_event_actor"))
    # Free text about the actor: a Discord user, a job name, a reason.
    actor_detail: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_property_id_created_at", "property_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"))
    # Markdown as written; rendered and sanitized on display (furnest.web.html).
    body_md: Mapped[str] = mapped_column(Text)
    actor: Mapped[Actor] = mapped_column(_actor_enum("note_actor"))
    # Free text about the actor, such as a Discord user.
    actor_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
