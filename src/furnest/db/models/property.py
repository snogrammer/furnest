"""Properties and their audit trail; the logic is in ``furnest.db.property_store``.

A property is the thing under review: one real unit, which one or more listings
(``listings.property_id``) advertise. Listings of the same unit on different
sources are grouped under one property (``furnest.db.grouping``). ``status`` is
its place in the pipeline (:class:`~furnest.domain.PropertyStatus`) and only
changes through ``SqlPropertyStore.transition``, which writes an ``audit_events``
row in the same transaction. The first event of every property has ``from_status``
NULL: its creation.

``score``, ``match_result`` and ``criteria_version_id`` hold the best match among
its listings (``match_listing_id`` names that listing), and ``snoozed_until``,
``manual_contact_required``, ``owner_type`` and the Discord message reference are set
by the pipeline and the Discord integration. ``owner_type`` is who is behind the unit
(:class:`~furnest.domain.OwnerType`): what the primary listing suggests, unless a
person set ``owner_type_override``, which wins and survives re-ingestion (#113).

When grouping finds that two properties are the same unit, one is merged into the
other: its listings, notes and emails move to the survivor and ``merged_into_id``
points there. The merged row stays, with its own audit trail, so old links and
Discord messages still resolve.

A :class:`ListingSeparation` records that a person split two listings apart: they
are different units, and grouping never puts them under one property again (until a
person merges their properties by hand, which deletes the separation).

A :class:`Note` is free-form markdown someone wrote about a property. Deny reasons
are notes too; the deny's audit event names its note in ``payload["note_id"]``.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow
from furnest.domain import Actor, OwnerType, PropertyStatus


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


def _owner_type_enum(name: str) -> Enum:
    return Enum(
        OwnerType,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda kinds: [k.value for k in kinds],
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
    # The listing ``match_result`` came from. Not a foreign key: it is only a hint
    # for which result a newer match replaces.
    match_listing_id: Mapped[int | None]
    criteria_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("criteria_versions.id", ondelete="SET NULL")
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    manual_contact_required: Mapped[bool] = mapped_column(default=False)
    # The effective owner type: ``owner_type_override`` when set, else what the primary
    # listing suggests. Kept current by ``property_store``, like manual_contact_required.
    owner_type: Mapped[OwnerType] = mapped_column(
        _owner_type_enum("property_owner_type"),
        default=OwnerType.UNKNOWN,
        server_default=OwnerType.UNKNOWN.value,
    )
    # Set by a person on the detail page; NULL means "use what the listing suggests".
    owner_type_override: Mapped[OwnerType | None] = mapped_column(
        _owner_type_enum("property_owner_type_override")
    )
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    discord_message_id: Mapped[str | None] = mapped_column(String(32))
    # Set once this property has been merged into another (grouping).
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", ondelete="SET NULL"), index=True
    )
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


class ListingSeparation(Base):
    """Two listings a person said are different units (a split); see the module docstring."""

    __tablename__ = "listing_separations"
    __table_args__ = (
        # One row per pair, stored lowest id first.
        CheckConstraint("listing_id < other_listing_id", name="ordered"),
        Index("ix_listing_separations_other_listing_id", "other_listing_id"),
    )

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    other_listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    actor: Mapped[Actor] = mapped_column(_actor_enum("listing_separation_actor"))
    actor_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
