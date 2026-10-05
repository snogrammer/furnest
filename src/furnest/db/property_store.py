"""SqlPropertyStore: properties, their status and the audit trail.

Until listings are grouped (P7), every listing gets its own property through
:meth:`SqlPropertyStore.ensure_for_listing`. A new property starts in ``new``, and
its creation is the first audit event.

:meth:`SqlPropertyStore.transition` is the only way to change a property's status.
In one transaction it checks the move against the state machine
(:func:`furnest.domain.check_transition`), writes the new status and records an
audit event with the actor. Every transaction takes SQLite's write lock up front
(``BEGIN IMMEDIATE``, see :mod:`furnest.db.engine`), so two clicks on the same
property run one after the other: the second reads the status the first wrote
and the state machine rejects it if it no longer applies. A property in
``unavailable`` can only be restored to the state it was in before; that state is
read from the audit trail here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import AuditEvent, Listing, Property
from furnest.db.session import session_scope
from furnest.dedup.address import UsAddressParser
from furnest.domain import Actor, AddressParser, NormalizedListing, PropertyStatus
from furnest.domain.status import check_transition


class PropertyNotFoundError(LookupError):
    def __init__(self, property_id: int) -> None:
        super().__init__(f"no property with id {property_id}")
        self.property_id = property_id


class ListingNotFoundError(LookupError):
    def __init__(self, listing_id: int) -> None:
        super().__init__(f"no listing with id {listing_id}")
        self.listing_id = listing_id


@dataclass(frozen=True)
class PropertyState:
    id: int
    status: PropertyStatus
    address: str | None
    lat: float | None
    lng: float | None
    score: int | None
    criteria_version_id: int | None
    snoozed_until: datetime | None
    manual_contact_required: bool
    status_changed_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    id: int
    property_id: int
    # None on the event that created the property.
    from_status: PropertyStatus | None
    to_status: PropertyStatus
    actor: Actor
    actor_detail: str | None
    payload: dict[str, Any] | None
    created_at: datetime


def _state(row: Property) -> PropertyState:
    return PropertyState(
        id=row.id,
        status=row.status,
        address=row.address,
        lat=row.lat,
        lng=row.lng,
        score=row.score,
        criteria_version_id=row.criteria_version_id,
        snoozed_until=row.snoozed_until,
        manual_contact_required=row.manual_contact_required,
        status_changed_at=row.status_changed_at,
        created_at=row.created_at,
    )


def _entry(row: AuditEvent) -> AuditEntry:
    return AuditEntry(
        id=row.id,
        property_id=row.property_id,
        from_status=row.from_status,
        to_status=row.to_status,
        actor=row.actor,
        actor_detail=row.actor_detail,
        payload=row.payload,
        created_at=row.created_at,
    )


def _status_before_unavailable(session: Session, property_id: int) -> PropertyStatus | None:
    """The ``from`` status of the latest move into ``unavailable``."""
    return session.scalars(
        select(AuditEvent.from_status)
        .where(
            AuditEvent.property_id == property_id,
            AuditEvent.to_status == PropertyStatus.UNAVAILABLE,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    ).first()


class SqlPropertyStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        address_parser: AddressParser | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._address_parser = address_parser or UsAddressParser()

    def ensure_for_listing(
        self, listing_id: int, *, at: datetime, actor: Actor = Actor.SYSTEM
    ) -> PropertyState:
        """The listing's property, created in ``new`` (with its audit event) if it has none."""
        with session_scope(self._session_factory) as session:
            listing_row = session.get(Listing, listing_id)
            if listing_row is None:
                raise ListingNotFoundError(listing_id)
            if listing_row.property_id is not None:
                return _state(session.get_one(Property, listing_row.property_id))
            listing = NormalizedListing.model_validate(listing_row.data)
            row = Property(
                status=PropertyStatus.NEW,
                address=self._canonical_address(listing),
                lat=listing_row.lat,
                lng=listing_row.lng,
                status_changed_at=at,
                created_at=at,
                updated_at=at,
            )
            session.add(row)
            session.flush()
            listing_row.property_id = row.id
            session.add(
                AuditEvent(
                    property_id=row.id,
                    from_status=None,
                    to_status=PropertyStatus.NEW,
                    actor=actor,
                    payload={"listing_id": listing_id},
                    created_at=at,
                )
            )
            return _state(row)

    def transition(
        self,
        property_id: int,
        to: PropertyStatus,
        *,
        actor: Actor,
        at: datetime,
        detail: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> PropertyState:
        """Move the property to ``to`` and record who did it.

        Raises :class:`~furnest.domain.InvalidTransitionError` if the state machine
        forbids the move and :class:`PropertyNotFoundError` if there is no such
        property. Nothing is written unless the whole transition succeeds.
        """
        with session_scope(self._session_factory) as session:
            row = session.get(Property, property_id)
            if row is None:
                raise PropertyNotFoundError(property_id)
            current = row.status
            restore_to = None
            if current is PropertyStatus.UNAVAILABLE:
                restore_to = _status_before_unavailable(session, property_id)
            check_transition(current, to, actor, restore_to=restore_to)
            row.status = to
            row.status_changed_at = at
            row.updated_at = at
            session.add(
                AuditEvent(
                    property_id=property_id,
                    from_status=current,
                    to_status=to,
                    actor=actor,
                    actor_detail=detail,
                    payload=None if payload is None else dict(payload),
                    created_at=at,
                )
            )
            return _state(row)

    def get(self, property_id: int) -> PropertyState | None:
        with session_scope(self._session_factory) as session:
            row = session.get(Property, property_id)
            return None if row is None else _state(row)

    def for_listing(self, listing_id: int) -> PropertyState | None:
        with session_scope(self._session_factory) as session:
            row = session.scalars(
                select(Property)
                .join(Listing, Listing.property_id == Property.id)
                .where(Listing.id == listing_id)
            ).first()
            return None if row is None else _state(row)

    def audit_events(self, property_id: int) -> list[AuditEntry]:
        """The property's audit trail, oldest first."""
        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.property_id == property_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
            return [_entry(row) for row in rows]

    def _canonical_address(self, listing: NormalizedListing) -> str | None:
        address = listing.address
        if not any((address.line1, address.line2, address.city, address.postal_code)):
            return None
        return self._address_parser.parse(address).canonical
