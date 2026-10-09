"""SqlPropertyStore: properties, their status and the audit trail.

A listing gets its property the first time it is matched (or through
:meth:`SqlPropertyStore.ensure_for_listing`). With grouping on (``GROUPING_*``), it
joins the property of another listing of the same unit when there is one
(:mod:`furnest.db.grouping`), merging properties that turn out to be one unit;
otherwise it gets a new property. A new property starts in ``new``, and its creation
is the first audit event. A property merged into another keeps its row with
``merged_into_id`` set: the views leave it out, its detail resolves to the survivor,
and changing its status raises :class:`PropertyMergedError`.

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

* :meth:`SqlPropertyStore.record_match` stores a listing's latest match, if it is
  the property's best (or replaces the result of the same listing). A passing
  match moves a ``new`` property to ``suggested`` and queues a :data:`NOTIFY` job in
  the same transaction. In every other state only the score changes, so re-running
  the criteria never undoes a decision a person made.
  It also keeps ``manual_contact_required`` in step with the primary listing: set
  while that listing has no email address, so the inquiry is sent by hand through
  the source (``furnest.email.inquiry``). ``owner_type`` is kept in step the same
  way: the primary listing's inferred owner type, unless a person set
  ``owner_type_override`` (:meth:`SqlPropertyStore.set_owner_type_override`), which
  wins until it is cleared.
* :meth:`SqlPropertyStore.mark_gone` moves a property to ``unavailable`` once none
  of its listings is active. A property whose status can't change that way (denied,
  won, lost, archived) gets a ``listings removed`` event in place instead, so the gap
  is on record either way. When a listing of a property with no other active listing
  comes back (:meth:`SqlPropertyStore.restore_relisted`), or a new listing is grouped
  into one (:mod:`furnest.db.grouping`), the unit is *relisted*: a ``relisted`` event
  records the listing, when the group went away and the price before and now, and an
  ``unavailable`` property moves back to the state it was in before.
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

Grouping mistakes are corrected by hand. :meth:`SqlPropertyStore.merge_properties`
merges one property into another, which keeps its status, and
:meth:`SqlPropertyStore.split_listing` moves a listing out to a new property in
``new``. Both are audited on both properties. A split is remembered so grouping
never undoes it (:mod:`furnest.db.grouping`), and the listings involved are scored
again by a ``rerun_criteria`` job, since each property shows its best listing's match.

A snooze isn't a status change, but who snoozed matters, so it is still written to
the audit trail as an event whose ``from_status`` and ``to_status`` are the same.

Once the notify job has posted a property to Discord
(:meth:`SqlPropertyStore.attach_message`), every status change and every snooze or
unsnooze also queues a :data:`MESSAGE_SYNC_JOB` (:func:`enqueue_sync`) in the same
transaction. It doesn't matter whether the change came from the dashboard, the API,
a button or the system: the message always ends up showing the property as it is.

:func:`transition_in`, :func:`deny_in` and :func:`snooze_in` do the same work as the
store methods, but inside a session the caller already holds. The Discord
interactions endpoint uses them, so a button's state change commits or rolls back
together with the record of the interaction.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, ScalarSelect, Select, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import GroupingSettings
from furnest.db.grouping import (
    after_grouping,
    audit_property_ids,
    group_listing,
    listing_ids_of,
    merge,
    resolve,
    separate,
    set_address_keys,
    unseparate,
)
from furnest.db.listing_store import PricePoint, price_of, price_points, stored_listing
from furnest.db.models import (
    AuditEvent,
    CriteriaVersion,
    JobStatus,
    Listing,
    Note,
    PriceHistory,
    Property,
)
from furnest.db.session import session_scope
from furnest.dedup.address import UsAddressParser
from furnest.dedup.grouping import AddressKey, GroupingThresholds
from furnest.domain import (
    Actor,
    AddressParser,
    Money,
    NormalizedListing,
    OwnerType,
    ParsedAddress,
    PropertyStatus,
)
from furnest.domain.price import Price
from furnest.domain.status import TRANSITIONS, check_transition
from furnest.matching import MatchResult
from furnest.pipeline.rerun import RERUN_CRITERIA
from furnest.worker import queue

# The job that tells the user about a suggested property: furnest.notify.post posts it.
NOTIFY = "notify"
# The job that redraws a property's Discord message from the database: furnest.notify.sync.
MESSAGE_SYNC_JOB = "discord.message_sync"
# The job that looks for the same unit on other sources: furnest.enrichment.alternatives.
ENRICH_ALTERNATIVES = "enrich.alternatives"
# The scam/risk check: furnest.enrichment.risk.
ENRICH_RISK = "enrich.risk"
# Jobs that haven't started yet. A running one may already have read the old state,
# so it doesn't count when deciding whether another notify or sync is needed.
_NOT_STARTED = (JobStatus.QUEUED, JobStatus.FAILED)


# The longest note body accepted, in characters, after stripping.
MAX_NOTE_LENGTH = 10_000


class PropertyNotFoundError(LookupError):
    def __init__(self, property_id: int) -> None:
        super().__init__(f"no property with id {property_id}")
        self.property_id = property_id


class PropertyMergedError(PropertyNotFoundError):
    """The property was merged into ``merged_into``; act on that one instead."""

    def __init__(self, property_id: int, merged_into: int) -> None:
        LookupError.__init__(self, f"property {property_id} was merged into {merged_into}")
        self.property_id = property_id
        self.merged_into = merged_into


class ListingNotFoundError(LookupError):
    def __init__(self, listing_id: int) -> None:
        super().__init__(f"no listing with id {listing_id}")
        self.listing_id = listing_id


class GroupingError(ValueError):
    """A manual merge or split that can't be done; the message says why."""


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
    # The match message in Discord, once the notify job has posted it.
    discord_message_id: str | None = None
    # Set once grouping merged this property into another.
    merged_into_id: int | None = None
    # Who is behind the unit: the override when set, else the primary listing's guess.
    owner_type: OwnerType = OwnerType.UNKNOWN
    owner_type_override: OwnerType | None = None


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
class PropertyPricePoint:
    """One price change of one of a property's listings."""

    listing_id: int
    source: str
    price: Price
    observed_at: datetime


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
    # Every listing's price changes in one timeline, oldest first.
    price_history: list[PropertyPricePoint] = field(default_factory=list)

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
        discord_message_id=row.discord_message_id,
        merged_into_id=row.merged_into_id,
        owner_type=row.owner_type,
        owner_type_override=row.owner_type_override,
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


def _live() -> ColumnElement[bool]:
    """Not merged into another property."""
    return Property.merged_into_id.is_(None)


def live_property(session: Session, property_id: int) -> Property:
    """The property, or :class:`PropertyNotFoundError` / :class:`PropertyMergedError`."""
    row = session.get(Property, property_id)
    if row is None:
        raise PropertyNotFoundError(property_id)
    if row.merged_into_id is not None:
        survivor = resolve(session, property_id)
        raise PropertyMergedError(property_id, survivor.id if survivor else row.merged_into_id)
    return row


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


def primary_listing(session: Session, property_id: int) -> Listing | None:
    """The property's primary listing: its lowest-id active one, else its lowest-id one."""
    return session.scalars(
        select(Listing).where(Listing.property_id == property_id).order_by(*_LISTING_ORDER)
    ).first()


def _manual_contact(listing_row: Listing) -> bool:
    """Whether the listing gives no email address, so the lister is reached through
    the source's own messaging (a ``platform_only`` contact, or none at all)."""
    return NormalizedListing.model_validate(listing_row.data).contact.email is None


def _inferred_owner_type(listing_row: Listing) -> OwnerType:
    """The owner type the listing's adapter inferred (see ``furnest.domain.owner_type``)."""
    return NormalizedListing.model_validate(listing_row.data).owner_type


def _effective_owner_type(row: Property, primary: Listing | None) -> OwnerType:
    if row.owner_type_override is not None:
        return row.owner_type_override
    return OwnerType.UNKNOWN if primary is None else _inferred_owner_type(primary)


def _sync_owner_type(session: Session, row: Property, primary: Listing | None) -> None:
    """Bring ``row.owner_type`` in step with the override and the primary listing,
    redrawing the Discord message when it changed."""
    kind = _effective_owner_type(row, primary)
    if kind is not row.owner_type:
        row.owner_type = kind
        _sync_message(session, row)


def _filtered[*Ts](
    statement: Select[*Ts],
    *,
    now: datetime,
    statuses: Iterable[PropertyStatus] | None,
    text: str | None,
    include_snoozed: bool,
    changed_since: datetime | None = None,
    owner_type: OwnerType | None = None,
) -> Select[*Ts]:
    """``statement`` over properties joined to their primary listing, filtered."""
    statement = (
        statement.select_from(Property)
        .outerjoin(Listing, Listing.id == _primary_listing_id())
        .where(_live())
    )
    if statuses is not None:
        statement = statement.where(Property.status.in_(list(statuses)))
    if not include_snoozed:
        statement = statement.where(_visible(now))
    if changed_since is not None:
        statement = statement.where(Property.status_changed_at >= changed_since)
    if owner_type is not None:
        statement = statement.where(Property.owner_type == owner_type)
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
    """The property's audit trail, oldest first, with those of properties merged into it."""
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.property_id.in_(audit_property_ids(session, property_id)))
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


def property_price_points(session: Session, property_id: int) -> list[PropertyPricePoint]:
    """The price changes of all the property's listings, oldest first.

    Merging moves listings to the survivor, so this covers merged properties too.
    """
    rows = session.execute(
        select(PriceHistory, Listing.source)
        .join(Listing, Listing.id == PriceHistory.listing_id)
        .where(Listing.property_id == property_id)
        .order_by(PriceHistory.observed_at, PriceHistory.id)
    )
    return [
        PropertyPricePoint(
            listing_id=row.listing_id,
            source=source,
            price=price_of(row),
            observed_at=row.observed_at,
        )
        for row, source in rows
    ]


def _criteria_version_id(session: Session, version: int | None) -> int | None:
    """The row id of criteria ``version``; a match records the version number."""
    if version is None:
        return None
    return session.scalars(
        select(CriteriaVersion.id).where(CriteriaVersion.version == version)
    ).first()


# Nothing left to do on these, so there is nothing to snooze.
FINISHED = frozenset({PropertyStatus.ARCHIVED, PropertyStatus.WON, PropertyStatus.LOST})


def enqueue_sync(
    session: Session, property_id: int, *, channel_id: str | None, message_id: str | None
) -> None:
    """Queue an edit of the message to match the property, unless one is already queued.

    Does nothing without a channel and message id, for example after a click that
    carried no message, or for a property that was never posted.
    """
    if channel_id is None or message_id is None:
        return
    queued = queue.pending(session, MESSAGE_SYNC_JOB, statuses=_NOT_STARTED, message_id=message_id)
    if queued is not None:
        return
    queue.enqueue(
        session,
        MESSAGE_SYNC_JOB,
        {"property_id": property_id, "channel_id": channel_id, "message_id": message_id},
    )


def _sync_message(session: Session, row: Property) -> None:
    """Queue a redraw of the property's Discord message, if it has one."""
    enqueue_sync(
        session, row.id, channel_id=row.discord_channel_id, message_id=row.discord_message_id
    )


def _enqueue_notify(session: Session, property_id: int) -> None:
    """Queue a :data:`NOTIFY` job for the property, unless one is already waiting."""
    if queue.pending(session, NOTIFY, statuses=_NOT_STARTED, property_id=property_id) is None:
        queue.enqueue(session, NOTIFY, {"property_id": property_id})


def _enqueue_enrichment(session: Session, property_id: int) -> None:
    """Queue the enrichment jobs for the property, unless one of each is already waiting."""
    for kind in (ENRICH_ALTERNATIVES, ENRICH_RISK):
        if queue.pending(session, kind, statuses=_NOT_STARTED, property_id=property_id) is None:
            queue.enqueue(session, kind, {"property_id": property_id})


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
    _sync_message(session, row)
    if to is PropertyStatus.APPROVED:
        _enqueue_enrichment(session, row.id)


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
    _sync_message(session, row)


def _has_active_listing(session: Session, property_id: int, *, besides: Iterable[int] = ()) -> bool:
    """Whether the property has an active listing other than ``besides``."""
    statement = select(Listing.id).where(
        Listing.property_id == property_id, Listing.removed_at.is_(None)
    )
    others = list(besides)
    if others:
        statement = statement.where(Listing.id.not_in(others))
    return session.scalars(statement.limit(1)).first() is not None


# actor_detail of the events that open and close a gap in a property's listings.
GONE = "listings removed"
RELISTED = "relisted"
# The in-place audit event of a person setting or clearing the owner type (#113).
OWNER_TYPE = "owner_type"


def _mark_gone_in(session: Session, row: Property, *, at: datetime) -> bool:
    """Move ``row`` to ``unavailable`` if none of its listings is active and the state
    machine allows it; whether it moved.

    A property that can't move there (denied, won, lost, archived) gets a ``listings
    removed`` event in place, so a later relisting knows how long the unit was gone.
    """
    if row.status is PropertyStatus.UNAVAILABLE or _has_active_listing(session, row.id):
        return False
    if PropertyStatus.UNAVAILABLE not in TRANSITIONS[row.status]:
        _audit_in_place(session, row, actor=Actor.SYSTEM, at=at, detail=GONE, payload={})
        return False
    _apply_transition(
        session,
        row,
        PropertyStatus.UNAVAILABLE,
        actor=Actor.SYSTEM,
        at=at,
        detail=GONE,
    )
    return True


def _gone_since(session: Session, property_id: int) -> datetime | None:
    """When the property's listings were last all removed, unless it was relisted since."""
    latest = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.property_id == property_id,
            AuditEvent.actor == Actor.SYSTEM,
            AuditEvent.actor_detail.in_([GONE, RELISTED]),
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    ).first()
    if latest is None or latest.actor_detail != GONE:
        return None
    return latest.created_at


def _price_before(session: Session, property_id: int, before: datetime) -> PriceHistory | None:
    """The property's last recorded price change before ``before``, on any listing."""
    return session.scalars(
        select(PriceHistory)
        .join(Listing, Listing.id == PriceHistory.listing_id)
        .where(Listing.property_id == property_id, PriceHistory.observed_at < before)
        .order_by(PriceHistory.observed_at.desc(), PriceHistory.id.desc())
        .limit(1)
    ).first()


def relist_in(session: Session, row: Property, listing_row: Listing, *, at: datetime) -> bool:
    """Record that ``listing_row`` brought the property's unit back after all of its
    listings were removed; whether the property's status changed.

    An ``unavailable`` property moves back to the state it left. Any other status is
    kept and the event is recorded in place.
    """
    payload: dict[str, Any] = {"listing_id": listing_row.id}
    gone_since = _gone_since(session, row.id)
    if gone_since is not None:
        payload["gone_since"] = gone_since.isoformat()
    now_minor, currency = listing_row.monthly_price_minor, listing_row.currency
    if now_minor is not None and currency is not None:
        payload["price_now_minor"] = now_minor
        payload["currency"] = currency
    before = _price_before(session, row.id, listing_row.last_seen)
    # Prices in two currencies aren't comparable; then only the current one is kept.
    if before is not None and payload.setdefault("currency", before.currency) == before.currency:
        payload["price_before_minor"] = before.monthly_minor
    if row.status is PropertyStatus.UNAVAILABLE:
        restore_to = _status_before_unavailable(session, row.id)
        if restore_to is not None:
            _apply_transition(
                session,
                row,
                restore_to,
                actor=Actor.SYSTEM,
                at=at,
                detail=RELISTED,
                payload=payload,
            )
            return True
    _audit_in_place(session, row, actor=Actor.SYSTEM, at=at, detail=RELISTED, payload=payload)
    return False


def _new_property(
    session: Session,
    listing_row: Listing,
    parsed: ParsedAddress | None,
    *,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Property:
    """A new property in ``new`` for ``listing_row``, with its creation audit event."""
    row = Property(
        status=PropertyStatus.NEW,
        address=None if parsed is None else parsed.canonical,
        lat=listing_row.lat,
        lng=listing_row.lng,
        manual_contact_required=_manual_contact(listing_row),
        owner_type=_inferred_owner_type(listing_row),
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
            actor_detail=detail,
            payload={"listing_id": listing_row.id, **(payload or {})},
            created_at=at,
        )
    )
    return row


def _enqueue_rescore(session: Session, listing_ids: list[int]) -> None:
    """Queue a ``rerun_criteria`` job scoring ``listing_ids`` against the active criteria."""
    version = session.scalars(
        select(CriteriaVersion.version).where(CriteriaVersion.is_active)
    ).first()
    if version is not None and listing_ids:
        queue.enqueue(
            session, RERUN_CRITERIA, {"criteria_version": version, "listing_ids": listing_ids}
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
    row = live_property(session, property_id)
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
    row = live_property(session, property_id)
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
    row = live_property(session, property_id)
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
        grouping: GroupingSettings | None = None,
    ) -> None:
        """``grouping`` turns on cross-source grouping; without it (or with it
        disabled) every listing gets its own property."""
        self._session_factory = session_factory
        self._address_parser = address_parser or UsAddressParser()
        self._grouping = grouping if grouping is not None and grouping.enabled else None
        self._thresholds = (
            None if self._grouping is None else GroupingThresholds.from_settings(self._grouping)
        )

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
            live_property(session, property_id)
            note = _add_note(session, property_id, text, actor=actor, at=at, detail=detail)
            return _note_entry(note)

    def notes(self, property_id: int) -> list[NoteEntry]:
        """The property's notes, oldest first."""
        with session_scope(self._session_factory) as session:
            return _notes(session, property_id)

    def set_owner_type_override(
        self,
        property_id: int,
        owner_type: OwnerType | None,
        *,
        actor: Actor,
        at: datetime,
        detail: str | None = None,
    ) -> PropertyState:
        """Set, or with ``None`` clear, the owner type a person chose for the property.

        The effective ``owner_type`` is recomputed (the override, else the primary
        listing's guess), an in-place audit event with ``actor_detail``
        :data:`OWNER_TYPE` records the change, and the property's listings are queued
        for a rescore so the owner-type rule sees it. Setting what is already set
        changes nothing. Raises :class:`PropertyNotFoundError` and
        :class:`PropertyMergedError` like :meth:`add_note`.
        """
        with session_scope(self._session_factory) as session:
            row = live_property(session, property_id)
            if row.owner_type_override == owner_type:
                return _state(row)
            row.owner_type_override = owner_type
            _sync_owner_type(session, row, primary_listing(session, row.id))
            row.updated_at = at
            _audit_in_place(
                session,
                row,
                actor=actor,
                at=at,
                detail=OWNER_TYPE,
                payload={
                    "owner_type_override": None if owner_type is None else owner_type.value,
                    "owner_type": row.owner_type.value,
                },
            )
            listing_ids = session.scalars(
                select(Listing.id).where(Listing.property_id == row.id).order_by(Listing.id)
            ).all()
            _enqueue_rescore(session, list(listing_ids))
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
            primary = primary_listing(session, row.id)
            manual = _manual_contact(primary) if primary is not None else False
            if manual != row.manual_contact_required:
                row.manual_contact_required = manual
                _sync_message(session, row)
            _sync_owner_type(session, row, primary)
            # The property shows its best listing's match; a listing's newer result
            # replaces its own older one even if lower.
            if (
                row.match_listing_id in (None, listing_id)
                or row.score is None
                or result.score > row.score
            ):
                row.score = result.score
                row.match_result = result.model_dump(mode="json")
                row.criteria_version_id = _criteria_version_id(session, result.criteria_version)
                row.match_listing_id = listing_id
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
                _enqueue_notify(session, row.id)
            return _state(row)

    def mark_gone(self, listing_ids: Iterable[int], *, at: datetime) -> list[int]:
        """Move the properties of ``listing_ids`` that have no active listing left to
        ``unavailable``; return the ids of the properties moved.

        A property that can't become unavailable (denied, won, lost, archived or
        already unavailable) is left alone.
        """
        with session_scope(self._session_factory) as session:
            return [
                row.id
                for row in self._properties_of(session, listing_ids)
                if _mark_gone_in(session, row, at=at)
            ]

    def restore_relisted(self, listing_ids: Iterable[int], *, at: datetime) -> list[int]:
        """Record the relisting of every property that ``listing_ids`` brought back
        and move the unavailable ones back to the state they were in before; return
        the ids of the properties moved.

        A property is back when it has no other active listing and its listings were
        recorded as all removed (``unavailable``, or a ``listings removed`` event not
        yet followed by a relisting), so calling this twice records it once.
        """
        ids = sorted(set(listing_ids))
        with session_scope(self._session_factory) as session:
            moved = []
            for row in self._properties_of(session, ids):
                gone = row.status is PropertyStatus.UNAVAILABLE or _gone_since(session, row.id)
                if gone and not _has_active_listing(session, row.id, besides=ids):
                    first = session.scalars(
                        select(Listing)
                        .where(Listing.property_id == row.id, Listing.id.in_(ids))
                        .order_by(Listing.id)
                        .limit(1)
                    ).one()
                    if relist_in(session, row, first, at=at):
                        moved.append(row.id)
                    continue
                if row.status is not PropertyStatus.UNAVAILABLE:
                    continue
                restore_to = _status_before_unavailable(session, row.id)
                if restore_to is None:  # never moved there through the store; nothing to restore
                    continue
                _apply_transition(
                    session, row, restore_to, actor=Actor.SYSTEM, at=at, detail=RELISTED
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
                    _enqueue_notify(session, row.id)
            return [row.id for row in rows]

    def attach_message(
        self, property_id: int, *, channel_id: str, message_id: str, rendered: PropertyState
    ) -> None:
        """Remember the Discord message just posted for the property.

        ``rendered`` is the state the message was drawn from. If the property changed
        while the message was being posted, a sync is queued so the message catches
        up. A message posted earlier (a notification repeated after a snooze) is
        synced too, which marks it as reposted.
        """
        with session_scope(self._session_factory) as session:
            row = session.get(Property, property_id)
            if row is None:
                raise PropertyNotFoundError(property_id)
            if row.discord_message_id != message_id:
                _sync_message(session, row)
            row.discord_channel_id = channel_id
            row.discord_message_id = message_id
            if (row.status, row.snoozed_until) != (rendered.status, rendered.snoozed_until):
                _sync_message(session, row)

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
        statement = (
            select(Property).where(_live()).order_by(Property.created_at.desc(), Property.id.desc())
        )
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
        changed_since: datetime | None = None,
        owner_type: OwnerType | None = None,
    ) -> PropertyPage:
        """One page of properties with their primary listing, for the dashboard.

        Filters as :meth:`query` does, plus ``text``: a case-insensitive substring
        of the listing title or the address (``%`` and ``_`` match themselves),
        ``changed_since``: only properties whose status changed at or after it, and
        ``owner_type``: only properties with that effective owner type.
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
                changed_since=changed_since,
                owner_type=owner_type,
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
        self,
        *,
        now: datetime,
        text: str | None = None,
        include_snoozed: bool = False,
        owner_type: OwnerType | None = None,
    ) -> dict[PropertyStatus, int]:
        """How many properties :meth:`search` would find in each status (absent: none)."""
        statement = _filtered(
            select(Property.status, func.count(Property.id)),
            now=now,
            statuses=None,
            text=text,
            include_snoozed=include_snoozed,
            owner_type=owner_type,
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
                price_history=property_price_points(session, property_id),
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

    def regroup(self, listing_ids: Iterable[int], *, at: datetime) -> list[int]:
        """Group ``listing_ids`` again, now that more is known about them (their photo
        hashes); return the ids of the properties merged away. Listings without a
        property are left for their first match."""
        if self._grouping is None or self._thresholds is None:
            return []
        merged: list[int] = []
        for listing_id in dict.fromkeys(listing_ids):
            with session_scope(self._session_factory) as session:
                listing_row = session.get(Listing, listing_id)
                if listing_row is None or listing_row.property_id is None:
                    continue
                self._set_keys(listing_row)
                outcome = group_listing(
                    session,
                    listing_row,
                    thresholds=self._thresholds,
                    max_candidates=self._grouping.max_candidates,
                    at=at,
                )
                merged.extend(outcome.merged)
        return merged

    def merge_properties(
        self,
        survivor_id: int,
        absorbed_id: int,
        *,
        actor: Actor,
        at: datetime,
        detail: str | None = None,
    ) -> PropertyState:
        """Merge property ``absorbed_id`` into ``survivor_id`` by hand.

        ``survivor_id`` keeps its status, and takes the other's listings, notes and
        emails as an automatic merge does (:func:`furnest.db.grouping.merge`). An
        ``absorbed_id`` merged away already stands for the property it went into. Any
        split between the two is forgotten: a person said they are one unit.

        Raises :class:`PropertyNotFoundError` for an unknown id,
        :class:`PropertyMergedError` if ``survivor_id`` was merged away and
        :class:`GroupingError` if both are already the same property.
        """
        with session_scope(self._session_factory) as session:
            survivor = live_property(session, survivor_id)
            absorbed = resolve(session, absorbed_id)
            if absorbed is None:
                raise PropertyNotFoundError(absorbed_id)
            if absorbed.id == survivor.id:
                raise GroupingError(f"#{absorbed_id} is already this property")
            members = listing_ids_of(session, [survivor.id, absorbed.id])
            unseparate(session, members[survivor.id], members[absorbed.id])
            merge(
                session,
                survivor,
                absorbed,
                at=at,
                actor=actor,
                payload={"manual": True} if detail is None else {"manual": True, "by": detail},
            )
            after_grouping(session, survivor, at=at, merged=True)
            return _state(survivor)

    def split_listing(
        self,
        property_id: int,
        listing_id: int,
        *,
        actor: Actor,
        at: datetime,
        detail: str | None = None,
    ) -> PropertyState:
        """Move listing ``listing_id`` out of property ``property_id`` to a new property
        in ``new``; return the new property.

        The listing is remembered as a different unit from every listing left behind,
        so grouping never puts them back together. The old property drops the match
        it took from the listing, and both are scored again (``rerun_criteria``); the
        old one becomes ``unavailable`` if it has no active listing left.

        Raises :class:`PropertyNotFoundError` / :class:`PropertyMergedError` as
        :meth:`transition` does, :class:`ListingNotFoundError` if the listing isn't
        the property's and :class:`GroupingError` if it is its only listing.
        """
        with session_scope(self._session_factory) as session:
            old = live_property(session, property_id)
            listing_row = session.get(Listing, listing_id)
            if listing_row is None or listing_row.property_id != old.id:
                raise ListingNotFoundError(listing_id)
            rest = sorted(listing_ids_of(session, [old.id])[old.id] - {listing_id})
            if not rest:
                raise GroupingError("a property's only listing can't be split off")
            separate(session, listing_id, rest, actor=actor, at=at, detail=detail)
            parsed = self._set_keys(listing_row)
            new = _new_property(
                session,
                listing_row,
                parsed,
                actor=actor,
                at=at,
                detail=detail,
                payload={"split_from": old.id},
            )
            session.flush()
            if old.match_listing_id == listing_id:
                old.score = None
                old.match_result = None
                old.criteria_version_id = None
                old.match_listing_id = None
            primary = primary_listing(session, old.id)
            if primary is not None:
                old_parsed = self._set_keys(primary)
                old.address = None if old_parsed is None else old_parsed.canonical
                old.lat, old.lng = primary.lat, primary.lng
                old.manual_contact_required = _manual_contact(primary)
                _sync_owner_type(session, old, primary)
            old.updated_at = at
            _audit_in_place(
                session,
                old,
                actor=actor,
                at=at,
                detail="split",
                payload={"listing_id": listing_id, "split_into": new.id},
            )
            _mark_gone_in(session, old, at=at)
            _enqueue_rescore(session, [listing_id, *rest])
            return _state(new)

    def _parsed(self, listing: NormalizedListing) -> ParsedAddress | None:
        address = listing.address
        if not any((address.line1, address.line2, address.city, address.postal_code)):
            return None
        return self._address_parser.parse(address)

    def _set_keys(self, listing_row: Listing) -> ParsedAddress | None:
        parsed = self._parsed(NormalizedListing.model_validate(listing_row.data))
        set_address_keys(listing_row, None if parsed is None else AddressKey.of(parsed))
        return parsed

    def _ensure(self, session: Session, listing_id: int, *, at: datetime, actor: Actor) -> Property:
        listing_row = session.get(Listing, listing_id)
        if listing_row is None:
            raise ListingNotFoundError(listing_id)
        parsed = self._set_keys(listing_row)
        if listing_row.property_id is not None:
            return session.get_one(Property, listing_row.property_id)
        if self._grouping is not None and self._thresholds is not None:
            outcome = group_listing(
                session,
                listing_row,
                thresholds=self._thresholds,
                max_candidates=self._grouping.max_candidates,
                at=at,
            )
            if outcome.property is not None:
                return outcome.property
        return _new_property(session, listing_row, parsed, actor=actor, at=at)

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
