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

The pipeline (:mod:`furnest.pipeline.properties`) drives the system's side of it:

* :meth:`SqlPropertyStore.record_match` stores a listing's latest match. A passing
  match moves a ``new`` property to ``suggested`` and queues a :data:`NOTIFY` job in
  the same transaction. In every other state only the score changes, so re-running
  the criteria never undoes a decision a person made.
* :meth:`SqlPropertyStore.mark_gone` moves a property to ``unavailable`` once none
  of its listings is active, and :meth:`SqlPropertyStore.restore_relisted` moves it
  back when one returns.
* :meth:`SqlPropertyStore.snooze` hides a property from the default views
  (:meth:`SqlPropertyStore.query`) until ``snoozed_until``.
  :meth:`SqlPropertyStore.unsnooze_expired` clears expired snoozes and queues the
  notification again for a property still waiting on a decision.

A snooze isn't a status change, but who snoozed matters, so it is still written to
the audit trail as an event whose ``from_status`` and ``to_status`` are the same.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import AuditEvent, CriteriaVersion, Listing, Property
from furnest.db.session import session_scope
from furnest.dedup.address import UsAddressParser
from furnest.domain import Actor, AddressParser, NormalizedListing, PropertyStatus
from furnest.domain.status import TRANSITIONS, check_transition
from furnest.matching import MatchResult
from furnest.worker import queue

# The job that tells the user about a suggested property; consumed by P5's Discord poster.
NOTIFY = "notify"


class PropertyNotFoundError(LookupError):
    def __init__(self, property_id: int) -> None:
        super().__init__(f"no property with id {property_id}")
        self.property_id = property_id


class ListingNotFoundError(LookupError):
    def __init__(self, listing_id: int) -> None:
        super().__init__(f"no listing with id {listing_id}")
        self.listing_id = listing_id


class SnoozeError(ValueError):
    def __init__(self, property_id: int, reason: str) -> None:
        super().__init__(f"can't snooze property {property_id}: {reason}")
        self.property_id = property_id
        self.reason = reason


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
    """The ``from`` status of the latest move into ``unavailable``.

    Events that didn't change the status (a snooze) don't count.
    """
    return session.scalars(
        select(AuditEvent.from_status)
        .where(
            AuditEvent.property_id == property_id,
            AuditEvent.to_status == PropertyStatus.UNAVAILABLE,
            AuditEvent.from_status != PropertyStatus.UNAVAILABLE,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    ).first()


def _criteria_version_id(session: Session, version: int | None) -> int | None:
    """The row id of criteria ``version``; a match records the version number."""
    if version is None:
        return None
    return session.scalars(
        select(CriteriaVersion.id).where(CriteriaVersion.version == version)
    ).first()


# Nothing left to do on these, so there is nothing to snooze.
_FINISHED = frozenset({PropertyStatus.ARCHIVED, PropertyStatus.WON, PropertyStatus.LOST})


def _apply_transition(
    session: Session,
    row: Property,
    to: PropertyStatus,
    *,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Check and apply one status change, with its audit event, in ``session``."""
    current = row.status
    restore_to = None
    if current is PropertyStatus.UNAVAILABLE:
        restore_to = _status_before_unavailable(session, row.id)
    check_transition(current, to, actor, restore_to=restore_to)
    row.status = to
    row.status_changed_at = at
    row.updated_at = at
    session.add(
        AuditEvent(
            property_id=row.id,
            from_status=current,
            to_status=to,
            actor=actor,
            actor_detail=detail,
            payload=None if payload is None else dict(payload),
            created_at=at,
        )
    )


def _audit_in_place(
    session: Session,
    row: Property,
    *,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
    payload: Mapping[str, Any],
) -> None:
    """Record something that happened to the property without a status change."""
    session.add(
        AuditEvent(
            property_id=row.id,
            from_status=row.status,
            to_status=row.status,
            actor=actor,
            actor_detail=detail,
            payload=dict(payload),
            created_at=at,
        )
    )


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
            return _state(self._ensure(session, listing_id, at=at, actor=actor))

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
            _apply_transition(session, row, to, actor=actor, at=at, detail=detail, payload=payload)
            return _state(row)

    def record_match(self, listing_id: int, result: MatchResult, *, at: datetime) -> PropertyState:
        """Store ``result`` on the listing's property, creating the property if needed.

        A passing result moves a ``new`` property to ``suggested`` and queues a
        :data:`NOTIFY` job for it. Any other state keeps its status: a property that
        is already suggested isn't notified twice, and one a person decided on stays
        decided whatever the score says now.
        """
        with session_scope(self._session_factory) as session:
            row = self._ensure(session, listing_id, at=at, actor=Actor.SYSTEM)
            row.score = result.score
            row.match_result = result.model_dump(mode="json")
            row.criteria_version_id = _criteria_version_id(session, result.criteria_version)
            row.updated_at = at
            if result.passed and row.status is PropertyStatus.NEW:
                _apply_transition(
                    session,
                    row,
                    PropertyStatus.SUGGESTED,
                    actor=Actor.SYSTEM,
                    at=at,
                    detail="match",
                    payload={"score": result.score, "criteria_version": result.criteria_version},
                )
                queue.enqueue(session, NOTIFY, {"property_id": row.id})
            return _state(row)

    def mark_gone(self, listing_ids: Iterable[int], *, at: datetime) -> list[int]:
        """Move the properties of ``listing_ids`` that have no active listing left to
        ``unavailable``; return the ids of the properties moved.

        A property that can't become unavailable (denied, won, lost, archived or
        already unavailable) is left alone.
        """
        with session_scope(self._session_factory) as session:
            moved = []
            for row in self._properties_of(session, listing_ids):
                if PropertyStatus.UNAVAILABLE not in TRANSITIONS[row.status]:
                    continue
                active = session.scalars(
                    select(Listing.id)
                    .where(Listing.property_id == row.id, Listing.removed_at.is_(None))
                    .limit(1)
                ).first()
                if active is not None:
                    continue
                _apply_transition(
                    session,
                    row,
                    PropertyStatus.UNAVAILABLE,
                    actor=Actor.SYSTEM,
                    at=at,
                    detail="listings removed",
                )
                moved.append(row.id)
            return moved

    def restore_relisted(self, listing_ids: Iterable[int], *, at: datetime) -> list[int]:
        """Move the unavailable properties of ``listing_ids`` back to the state they
        were in before; return the ids of the properties moved."""
        with session_scope(self._session_factory) as session:
            moved = []
            for row in self._properties_of(session, listing_ids):
                if row.status is not PropertyStatus.UNAVAILABLE:
                    continue
                restore_to = _status_before_unavailable(session, row.id)
                if restore_to is None:  # never moved there through the store; nothing to restore
                    continue
                _apply_transition(
                    session, row, restore_to, actor=Actor.SYSTEM, at=at, detail="relisted"
                )
                moved.append(row.id)
            return moved

    def snooze(
        self,
        property_id: int,
        *,
        until: datetime,
        actor: Actor,
        at: datetime,
        detail: str | None = None,
    ) -> PropertyState:
        """Hide the property from the default views until ``until``.

        Snoozing again replaces the previous ``until``. Raises :class:`SnoozeError`
        for a time that isn't in the future or a property with nothing left to do
        (archived, won or lost).
        """
        if until <= at:
            raise SnoozeError(property_id, "the snooze must end in the future")
        with session_scope(self._session_factory) as session:
            row = session.get(Property, property_id)
            if row is None:
                raise PropertyNotFoundError(property_id)
            if row.status in _FINISHED:
                raise SnoozeError(property_id, f"it is {row.status}")
            row.snoozed_until = until
            row.updated_at = at
            _audit_in_place(
                session,
                row,
                actor=actor,
                at=at,
                detail=detail,
                payload={"snoozed_until": until.isoformat()},
            )
            return _state(row)

    def unsnooze_expired(self, *, now: datetime) -> list[int]:
        """Clear every snooze that ended by ``now``; return the property ids.

        A property still waiting on a decision (``suggested``) gets a new
        :data:`NOTIFY` job, so it comes back to the user rather than only to the
        dashboard's default view.
        """
        with session_scope(self._session_factory) as session:
            rows = list(
                session.scalars(
                    select(Property)
                    .where(Property.snoozed_until.is_not(None), Property.snoozed_until <= now)
                    .order_by(Property.id)
                )
            )
            for row in rows:
                row.snoozed_until = None
                row.updated_at = now
                _audit_in_place(
                    session, row, actor=Actor.SYSTEM, at=now, payload={"unsnoozed": True}
                )
                if row.status is PropertyStatus.SUGGESTED:
                    queue.enqueue(session, NOTIFY, {"property_id": row.id})
            return [row.id for row in rows]

    def query(
        self,
        *,
        now: datetime,
        statuses: Iterable[PropertyStatus] | None = None,
        include_snoozed: bool = False,
    ) -> list[PropertyState]:
        """Properties, newest first, optionally only in ``statuses``.

        Properties snoozed past ``now`` are left out unless ``include_snoozed``: this
        is the dashboard's default view.
        """
        statement = select(Property).order_by(Property.created_at.desc(), Property.id.desc())
        if statuses is not None:
            statement = statement.where(Property.status.in_(list(statuses)))
        if not include_snoozed:
            statement = statement.where(
                Property.snoozed_until.is_(None) | (Property.snoozed_until <= now)
            )
        with session_scope(self._session_factory) as session:
            return [_state(row) for row in session.scalars(statement)]

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

    def _ensure(self, session: Session, listing_id: int, *, at: datetime, actor: Actor) -> Property:
        listing_row = session.get(Listing, listing_id)
        if listing_row is None:
            raise ListingNotFoundError(listing_id)
        if listing_row.property_id is not None:
            return session.get_one(Property, listing_row.property_id)
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
        return row

    @staticmethod
    def _properties_of(session: Session, listing_ids: Iterable[int]) -> list[Property]:
        return list(
            session.scalars(
                select(Property)
                .where(
                    Property.id.in_(
                        select(Listing.property_id).where(Listing.id.in_(list(listing_ids)))
                    )
                )
                .order_by(Property.id)
            )
        )
