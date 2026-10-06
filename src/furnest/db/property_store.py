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
  (:meth:`SqlPropertyStore.query` and :meth:`SqlPropertyStore.search`) until
  ``snoozed_until``.
  :meth:`SqlPropertyStore.unsnooze_expired` clears expired snoozes and queues the
  notification again for a property still waiting on a decision.

The detail page reads one property whole through :meth:`SqlPropertyStore.detail`.
The dashboard reads through :meth:`SqlPropertyStore.search`, one page at a time,
and :meth:`SqlPropertyStore.status_counts`. Each row carries a summary of the
property's primary listing: its lowest-id active listing, or its lowest-id listing
if none is active. Text search matches that listing's title and the property's
address.

Notes (:meth:`SqlPropertyStore.add_note`, :meth:`SqlPropertyStore.notes`) are
free-form markdown about a property. :meth:`SqlPropertyStore.deny` denies a
property and, given a reason, saves it as a note in the same transaction; the
audit event points at the note with ``payload["note_id"]``.

A snooze isn't a status change, but who snoozed matters, so it is still written to
the audit trail as an event whose ``from_status`` and ``to_status`` are the same.

:func:`transition_in`, :func:`deny_in` and :func:`snooze_in` do the same work as the
store methods, but inside a session the caller already holds. The Discord
interactions endpoint uses them, so a button's state change commits or rolls back
together with the record of the interaction.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, ScalarSelect, Select, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import PricePoint, price_points, stored_listing
from furnest.db.models import AuditEvent, CriteriaVersion, Listing, Note, Property
from furnest.db.session import session_scope
from furnest.dedup.address import UsAddressParser
from furnest.domain import Actor, AddressParser, Money, NormalizedListing, PropertyStatus
from furnest.domain.status import TRANSITIONS, check_transition
from furnest.matching import MatchResult
from furnest.worker import queue

# The job that tells the user about a suggested property; consumed by P5's Discord poster.
NOTIFY = "notify"


# The longest note body accepted, in characters, after stripping.
MAX_NOTE_LENGTH = 10_000


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


@dataclass(frozen=True)
class NoteEntry:
    id: int
    property_id: int
    # Markdown, unrendered.
    body_md: str
    actor: Actor
    actor_detail: str | None
    created_at: datetime


@dataclass(frozen=True)
class ListingSummary:
    """What a list of properties shows about the listing behind each one."""

    id: int
    source: str
    url: str
    title: str
    monthly_price: Money | None
    beds: int | None
    baths: float | None
    removed_at: datetime | None


@dataclass(frozen=True)
class PropertyRow:
    property: PropertyState
    # None only for a property whose listings were all deleted.
    listing: ListingSummary | None


@dataclass(frozen=True)
class PropertyPage:
    rows: list[PropertyRow]
    # Matching properties across all pages.
    total: int


@dataclass(frozen=True)
class ListingDetail:
    """One of a property's listings, whole, for the detail page."""

    summary: ListingSummary
    # As the adapter produced it: description, photos, sqft, first and last seen.
    listing: NormalizedListing
    price_history: list[PricePoint]


@dataclass(frozen=True)
class PropertyDetail:
    property: PropertyState
    # None until the property's listing has been matched, or if the stored result
    # no longer reads as a MatchResult.
    match: MatchResult | None
    # In _LISTING_ORDER, so the primary listing comes first.
    listings: list[ListingDetail]
    # Oldest first.
    audit: list[AuditEntry]
    # Oldest first.
    notes: list[NoteEntry]

    # ``property`` above shadows the builtin in this class body.
    @builtins.property
    def primary(self) -> ListingDetail | None:
        return self.listings[0] if self.listings else None

    @builtins.property
    def images(self) -> list[str]:
        """Every listing's photos, primary listing's first, each URL once."""
        return list(
            dict.fromkeys(str(url) for detail in self.listings for url in detail.listing.images)
        )


class PropertySort(StrEnum):
    SCORE = "score"  # best first; unscored last
    PRICE = "price"  # cheapest monthly price first; unknown last
    NEWEST = "newest"


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


def _summary(row: Listing) -> ListingSummary:
    price = None
    if row.monthly_price_minor is not None and row.currency is not None:
        price = Money(amount_minor=row.monthly_price_minor, currency=row.currency)
    return ListingSummary(
        id=row.id,
        source=row.source,
        url=row.url,
        title=row.title,
        monthly_price=price,
        beds=row.beds,
        baths=row.baths,
        removed_at=row.removed_at,
    )


def _visible(now: datetime) -> ColumnElement[bool]:
    """Not snoozed past ``now``."""
    return Property.snoozed_until.is_(None) | (Property.snoozed_until <= now)


# Active listings first, then by id; the first one is the property's primary listing.
_LISTING_ORDER = (Listing.removed_at.is_not(None), Listing.id)


def _primary_listing_id() -> ScalarSelect[int]:
    return (
        select(Listing.id)
        .where(Listing.property_id == Property.id)
        .order_by(*_LISTING_ORDER)
        .limit(1)
        .correlate(Property)
        .scalar_subquery()
    )


def _filtered[*Ts](
    statement: Select[*Ts],
    *,
    now: datetime,
    statuses: Iterable[PropertyStatus] | None,
    text: str | None,
    include_snoozed: bool,
) -> Select[*Ts]:
    """``statement`` over properties joined to their primary listing, filtered."""
    statement = statement.select_from(Property).outerjoin(
        Listing, Listing.id == _primary_listing_id()
    )
    if statuses is not None:
        statement = statement.where(Property.status.in_(list(statuses)))
    if not include_snoozed:
        statement = statement.where(_visible(now))
    if text:
        statement = statement.where(
            or_(
                Listing.title.icontains(text, autoescape=True),
                Property.address.icontains(text, autoescape=True),
            )
        )
    return statement


_ORDER = {
    PropertySort.SCORE: (Property.score.desc().nulls_last(),),
    PropertySort.PRICE: (Listing.monthly_price_minor.asc().nulls_last(),),
    PropertySort.NEWEST: (),
}


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


def _audit(session: Session, property_id: int) -> list[AuditEntry]:
    """The property's audit trail, oldest first."""
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.property_id == property_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return [_entry(row) for row in rows]


def _note_entry(row: Note) -> NoteEntry:
    return NoteEntry(
        id=row.id,
        property_id=row.property_id,
        body_md=row.body_md,
        actor=row.actor,
        actor_detail=row.actor_detail,
        created_at=row.created_at,
    )


def _notes(session: Session, property_id: int) -> list[NoteEntry]:
    """The property's notes, oldest first."""
    rows = session.scalars(
        select(Note).where(Note.property_id == property_id).order_by(Note.created_at, Note.id)
    )
    return [_note_entry(row) for row in rows]


def note_body(text: str) -> str:
    """``text`` stripped, or :class:`ValueError` if that is empty or too long."""
    body = text.strip()
    if not body:
        raise ValueError("a note can't be empty")
    if len(body) > MAX_NOTE_LENGTH:
        raise ValueError(f"a note can't be longer than {MAX_NOTE_LENGTH} characters")
    return body


def _add_note(
    session: Session,
    property_id: int,
    body: str,
    *,
    actor: Actor,
    at: datetime,
    detail: str | None,
) -> Note:
    row = Note(
        property_id=property_id, body_md=body, actor=actor, actor_detail=detail, created_at=at
    )
    session.add(row)
    session.flush()
    return row


def _match(data: dict[str, Any] | None) -> MatchResult | None:
    if data is None:
        return None
    try:
        return MatchResult.model_validate(data)
    except ValidationError:
        return None


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
FINISHED = frozenset({PropertyStatus.ARCHIVED, PropertyStatus.WON, PropertyStatus.LOST})


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


def transition_in(
    session: Session,
    property_id: int,
    to: PropertyStatus,
    *,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> PropertyState:
    """:meth:`SqlPropertyStore.transition` inside the caller's ``session``."""
    row = session.get(Property, property_id)
    if row is None:
        raise PropertyNotFoundError(property_id)
    _apply_transition(session, row, to, actor=actor, at=at, detail=detail, payload=payload)
    return _state(row)


def deny_in(
    session: Session,
    property_id: int,
    *,
    actor: Actor,
    at: datetime,
    reason: str | None = None,
    detail: str | None = None,
) -> PropertyState:
    """:meth:`SqlPropertyStore.deny` inside the caller's ``session``.

    The note is written before the transition is checked, so both run in a savepoint:
    a refused deny leaves no note behind even if the caller catches the error and
    commits.
    """
    body = None if reason is None or not reason.strip() else note_body(reason)
    row = session.get(Property, property_id)
    if row is None:
        raise PropertyNotFoundError(property_id)
    with session.begin_nested():
        payload = None
        if body is not None:
            note = _add_note(session, property_id, body, actor=actor, at=at, detail=detail)
            payload = {"note_id": note.id}
        _apply_transition(
            session,
            row,
            PropertyStatus.DENIED,
            actor=actor,
            at=at,
            detail=detail,
            payload=payload,
        )
    return _state(row)


def snooze_in(
    session: Session,
    property_id: int,
    *,
    until: datetime,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
) -> PropertyState:
    """:meth:`SqlPropertyStore.snooze` inside the caller's ``session``."""
    if until <= at:
        raise SnoozeError(property_id, "the snooze must end in the future")
    row = session.get(Property, property_id)
    if row is None:
        raise PropertyNotFoundError(property_id)
    if row.status in FINISHED:
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
            return transition_in(
                session, property_id, to, actor=actor, at=at, detail=detail, payload=payload
            )

    def deny(
        self,
        property_id: int,
        *,
        actor: Actor,
        at: datetime,
        reason: str | None = None,
        detail: str | None = None,
    ) -> PropertyState:
        """Move the property to ``denied``, saving a non-blank ``reason`` as a note.

        Raises like :meth:`transition`, and :class:`ValueError` if the reason is
        too long. Nothing is written unless the whole deny succeeds.
        """
        with session_scope(self._session_factory) as session:
            return deny_in(session, property_id, actor=actor, at=at, reason=reason, detail=detail)

    def add_note(
        self,
        property_id: int,
        body: str,
        *,
        actor: Actor,
        at: datetime,
        detail: str | None = None,
    ) -> NoteEntry:
        """Add a markdown note to the property.

        Raises :class:`ValueError` for an empty or too long body and
        :class:`PropertyNotFoundError` if there is no such property.
        """
        text = note_body(body)
        with session_scope(self._session_factory) as session:
            if session.get(Property, property_id) is None:
                raise PropertyNotFoundError(property_id)
            note = _add_note(session, property_id, text, actor=actor, at=at, detail=detail)
            return _note_entry(note)

    def notes(self, property_id: int) -> list[NoteEntry]:
        """The property's notes, oldest first."""
        with session_scope(self._session_factory) as session:
            return _notes(session, property_id)

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
        with session_scope(self._session_factory) as session:
            return snooze_in(session, property_id, until=until, actor=actor, at=at, detail=detail)

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
            statement = statement.where(_visible(now))
        with session_scope(self._session_factory) as session:
            return [_state(row) for row in session.scalars(statement)]

    def search(
        self,
        *,
        now: datetime,
        limit: int,
        offset: int = 0,
        statuses: Iterable[PropertyStatus] | None = None,
        text: str | None = None,
        sort: PropertySort = PropertySort.SCORE,
        include_snoozed: bool = False,
    ) -> PropertyPage:
        """One page of properties with their primary listing, for the dashboard.

        Filters as :meth:`query` does, plus ``text``: a case-insensitive substring
        of the listing title or the address (``%`` and ``_`` match themselves).
        Ties in ``sort`` fall back to newest first, so paging is stable.
        """
        statuses = None if statuses is None else list(statuses)

        def filtered[*Ts](statement: Select[*Ts]) -> Select[*Ts]:
            return _filtered(
                statement,
                now=now,
                statuses=statuses,
                text=text,
                include_snoozed=include_snoozed,
            )

        statement = (
            filtered(select(Property, Listing))
            .order_by(*_ORDER[sort], Property.created_at.desc(), Property.id.desc())
            .limit(limit)
            .offset(offset)
        )
        with session_scope(self._session_factory) as session:
            total = session.scalar(filtered(select(func.count(Property.id)))) or 0
            rows = [
                PropertyRow(
                    property=_state(prop), listing=None if listing is None else _summary(listing)
                )
                for prop, listing in session.execute(statement)
            ]
            return PropertyPage(rows=rows, total=total)

    def status_counts(
        self, *, now: datetime, text: str | None = None, include_snoozed: bool = False
    ) -> dict[PropertyStatus, int]:
        """How many properties :meth:`search` would find in each status (absent: none)."""
        statement = _filtered(
            select(Property.status, func.count(Property.id)),
            now=now,
            statuses=None,
            text=text,
            include_snoozed=include_snoozed,
        ).group_by(Property.status)
        with session_scope(self._session_factory) as session:
            return {status: count for status, count in session.execute(statement)}

    def get(self, property_id: int) -> PropertyState | None:
        with session_scope(self._session_factory) as session:
            row = session.get(Property, property_id)
            return None if row is None else _state(row)

    def detail(self, property_id: int) -> PropertyDetail | None:
        """Everything stored about one property, read in one transaction."""
        with session_scope(self._session_factory) as session:
            row = session.get(Property, property_id)
            if row is None:
                return None
            listing_rows = session.scalars(
                select(Listing).where(Listing.property_id == property_id).order_by(*_LISTING_ORDER)
            )
            listings = [
                ListingDetail(
                    summary=_summary(listing),
                    listing=stored_listing(listing).listing,
                    price_history=price_points(session, listing.id),
                )
                for listing in listing_rows
            ]
            return PropertyDetail(
                property=_state(row),
                match=_match(row.match_result),
                listings=listings,
                audit=_audit(session, property_id),
                notes=_notes(session, property_id),
            )

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
            return _audit(session, property_id)

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
