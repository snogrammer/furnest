"""Grouping listings of the same unit under one property, and merging properties.

:func:`group_listing` runs inside the property store's transaction whenever a
listing is matched (:meth:`SqlPropertyStore.record_match`), before the match can
notify anyone, and again once a listing's photos are hashed
(:meth:`SqlPropertyStore.regroup`), since pHashes usually arrive after the first
ingestion.

1. **Candidates** are live properties (not merged away) with a listing at the same
   address (``listings.address_key`` / ``street_key``) or within ``max_distance_m``
   (a lat/lng box on ``ix_listings_lat_lng``), most recent first, at most
   ``max_candidates``.
2. Each candidate's listings are compared with :func:`~furnest.dedup.grouping.score_pair`;
   a property matches when one of its listings does.
3. The listing's own property (if it has one) and every matching property are the
   same unit. The one furthest along the pipeline survives
   (:func:`~furnest.domain.status_rank`; an ``unavailable`` property counts as the
   status it left), ties going to the oldest. It keeps its status, so a merge never
   undoes a decision or moves a property outside the state machine. A new listing
   joining a property that was ``suggested``, ``denied`` or further isn't notified
   again: that is the point.
4. Every other one is merged into it (:func:`merge`): listings, notes and emails move,
   ``merged_into_id`` is set, and both properties get a ``merged`` audit event with
   the score and signals. The merged property keeps its own audit trail, which the
   detail page shows alongside the survivor's. Its Discord message is redrawn as
   merged with its buttons off, unless the survivor has no message, in which case
   the survivor takes it over.
5. A new listing joining a unit whose listings were all removed (a repost under a new
   id) relists it (``relist_in``): a ``relisted`` event after the ``grouped`` one, and
   an ``unavailable`` survivor moves back to the state it left. A survivor left
   ``unavailable`` with an active listing after a merge is restored the same way.

A person can correct it from the dashboard (``SqlPropertyStore.merge_properties`` and
``split_listing``). A manual merge is never undone here: grouping only merges. A
split is remembered as :class:`~furnest.db.models.ListingSeparation` pairs, and
grouping never puts two separated listings under one property: a candidate holding
a listing separated from the one being grouped is skipped, and so is any candidate
separated from the group taken so far (best match first), so a new listing matching
both halves of a split joins only one.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from furnest.db.models import Email, Listing, ListingImage, ListingSeparation, Note, Property
from furnest.dedup.grouping import (
    AddressKey,
    GroupingThresholds,
    ListingFacts,
    PairScore,
    score_pair,
)
from furnest.domain import Actor, NormalizedListing, PropertyStatus, status_rank

# Meters per degree of latitude, for the candidate search box.
_M_PER_DEGREE = 111_320.0
# How far a merged_into_id chain is followed; merges re-point chains, so one hop
# is the norm and this only guards against a cycle.
_MAX_HOPS = 16


@dataclass(frozen=True)
class GroupingOutcome:
    """What :func:`group_listing` did."""

    property: Property | None
    # Ids of the properties merged into ``property``.
    merged: tuple[int, ...] = ()


def set_address_keys(listing_row: Listing, key: AddressKey | None) -> None:
    """Store the listing's address keys for candidate lookups."""
    listing_row.address_key = key.canonical if key else None
    listing_row.street_key = key.street if key else None


def resolve(session: Session, property_id: int) -> Property | None:
    """The live property ``property_id`` is, following merges; None if there is none."""
    row = session.get(Property, property_id)
    for _ in range(_MAX_HOPS):
        if row is None or row.merged_into_id is None:
            return row
        row = session.get(Property, row.merged_into_id)
    return row


def merged_ids(session: Session, property_id: int) -> list[int]:
    """The ids of every property merged into ``property_id``, directly or not."""
    found: list[int] = []
    frontier = [property_id]
    while frontier and len(found) < 10_000:
        children = list(
            session.scalars(select(Property.id).where(Property.merged_into_id.in_(frontier)))
        )
        found.extend(children)
        frontier = children
    return found


def separations(session: Session, listing_ids: Collection[int]) -> set[frozenset[int]]:
    """The separated pairs among ``listing_ids``."""
    ids = list(listing_ids)
    if len(ids) < 2:
        return set()
    rows = session.execute(
        select(ListingSeparation.listing_id, ListingSeparation.other_listing_id).where(
            ListingSeparation.listing_id.in_(ids), ListingSeparation.other_listing_id.in_(ids)
        )
    )
    return {frozenset(pair) for pair in rows}


def separate(
    session: Session,
    listing_id: int,
    others: Collection[int],
    *,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
) -> None:
    """Record that ``listing_id`` is a different unit from each of ``others``."""
    known = separations(session, [listing_id, *others])
    for other in others:
        if other == listing_id or frozenset((listing_id, other)) in known:
            continue
        low, high = sorted((listing_id, other))
        session.add(
            ListingSeparation(
                listing_id=low,
                other_listing_id=high,
                actor=actor,
                actor_detail=detail,
                created_at=at,
            )
        )


def unseparate(session: Session, listing_ids: Collection[int], others: Collection[int]) -> None:
    """Forget every separation between a listing of ``listing_ids`` and one of ``others``."""
    ids, other_ids = list(listing_ids), list(others)
    session.execute(
        delete(ListingSeparation).where(
            or_(
                and_(
                    ListingSeparation.listing_id.in_(ids),
                    ListingSeparation.other_listing_id.in_(other_ids),
                ),
                and_(
                    ListingSeparation.listing_id.in_(other_ids),
                    ListingSeparation.other_listing_id.in_(ids),
                ),
            )
        )
    )


def listing_ids_of(session: Session, property_ids: Collection[int]) -> dict[int, set[int]]:
    """Each property's listing ids."""
    found: dict[int, set[int]] = {property_id: set() for property_id in property_ids}
    rows = session.execute(
        select(Listing.property_id, Listing.id).where(Listing.property_id.in_(list(property_ids)))
    )
    for property_id, listing_id in rows:
        if property_id is not None:
            found[property_id].add(listing_id)
    return found


def _facts(listing_row: Listing, phashes: tuple[str, ...]) -> ListingFacts:
    listing = NormalizedListing.model_validate(listing_row.data)
    return ListingFacts(
        listing_id=listing_row.id,
        address=AddressKey.stored(listing_row.address_key, listing_row.street_key),
        lat=listing_row.lat,
        lng=listing_row.lng,
        beds=listing_row.beds,
        phashes=phashes,
        title=listing_row.title,
        description=listing.description,
    )


def _phashes(session: Session, listing_ids: list[int]) -> dict[int, tuple[str, ...]]:
    rows = session.execute(
        select(ListingImage.listing_id, ListingImage.phash)
        .where(ListingImage.listing_id.in_(listing_ids), ListingImage.phash.is_not(None))
        .order_by(ListingImage.listing_id, ListingImage.position)
    )
    hashes: dict[int, list[str]] = defaultdict(list)
    for listing_id, phash in rows:
        if phash is not None:
            hashes[listing_id].append(phash)
    return {listing_id: tuple(values) for listing_id, values in hashes.items()}


def _candidates(
    session: Session,
    listing_row: Listing,
    *,
    exclude: int | None,
    thresholds: GroupingThresholds,
    limit: int,
) -> list[Property]:
    near: list[Any] = []
    if listing_row.address_key is not None:
        near.append(Listing.address_key == listing_row.address_key)
    if listing_row.street_key is not None:
        near.append(Listing.street_key == listing_row.street_key)
    if listing_row.lat is not None and listing_row.lng is not None:
        d_lat = thresholds.max_distance_m / _M_PER_DEGREE
        cos = max(math.cos(math.radians(listing_row.lat)), 0.01)
        d_lng = d_lat / cos
        near.append(
            and_(
                Listing.lat.between(listing_row.lat - d_lat, listing_row.lat + d_lat),
                Listing.lng.between(listing_row.lng - d_lng, listing_row.lng + d_lng),
            )
        )
    if not near:
        return []
    statement = (
        select(Property)
        .join(Listing, Listing.property_id == Property.id)
        .where(or_(*near), Property.merged_into_id.is_(None), Listing.id != listing_row.id)
        .distinct()
        .order_by(Property.id.desc())
        .limit(limit)
    )
    if exclude is not None:
        statement = statement.where(Property.id != exclude)
    return list(session.scalars(statement))


def _best_matches(
    session: Session,
    listing_row: Listing,
    candidates: list[Property],
    thresholds: GroupingThresholds,
) -> dict[int, PairScore]:
    """The best-scoring matching pair for each candidate property that matches."""
    if not candidates:
        return {}
    ids = [row.id for row in candidates]
    others = list(
        session.scalars(
            select(Listing).where(Listing.property_id.in_(ids), Listing.id != listing_row.id)
        )
    )
    hashes = _phashes(session, [listing_row.id, *(row.id for row in others)])
    facts = _facts(listing_row, hashes.get(listing_row.id, ()))
    best: dict[int, PairScore] = {}
    for other in others:
        property_id = other.property_id
        pair = score_pair(facts, _facts(other, hashes.get(other.id, ())), thresholds)
        if property_id is None or not pair.matched:
            continue
        current = best.get(property_id)
        if current is None or pair.score > current.score:
            best[property_id] = pair
    return best


def _effective_status(session: Session, row: Property) -> PropertyStatus:
    """``row.status``, or for an unavailable property the status it left."""
    from furnest.db.property_store import _status_before_unavailable

    if row.status is PropertyStatus.UNAVAILABLE:
        return _status_before_unavailable(session, row.id) or PropertyStatus.UNAVAILABLE
    return row.status


def _survivor(session: Session, rows: list[Property]) -> Property:
    def key(row: Property) -> tuple[int, bool, int]:
        return (
            status_rank(_effective_status(session, row)),
            row.status is not PropertyStatus.UNAVAILABLE,
            -row.id,
        )

    return max(rows, key=key)


def _compatible(
    session: Session,
    listing_id: int,
    own: Property | None,
    candidates: list[Property],
    matches: Mapping[int, PairScore],
) -> list[Property]:
    """The matching candidates that can join ``own`` (and the listing) without putting
    two separated listings together, best match first."""
    matching = sorted(
        (row for row in candidates if row.id in matches),
        key=lambda row: matches[row.id].score,
        reverse=True,
    )
    if not matching:
        return []
    members = listing_ids_of(
        session, [row.id for row in matching] + ([] if own is None else [own.id])
    )
    taken = {listing_id} | (set() if own is None else members[own.id])
    apart = separations(session, taken.union(*(members[row.id] for row in matching)))
    if not apart:
        return matching
    group = []
    for row in matching:
        if any(frozenset((a, b)) in apart for a in taken for b in members[row.id]):
            continue
        group.append(row)
        taken |= members[row.id]
    return group


def group_listing(
    session: Session,
    listing_row: Listing,
    *,
    thresholds: GroupingThresholds,
    max_candidates: int,
    at: datetime,
) -> GroupingOutcome:
    """Find the listing's property among the others and merge what is the same unit.

    Returns the property the listing now belongs to, or ``None`` property when it has
    none yet and nothing matched (the caller creates one).
    """
    from furnest.db.property_store import _audit_in_place, _has_active_listing, relist_in

    own = None if listing_row.property_id is None else resolve(session, listing_row.property_id)
    candidates = _candidates(
        session,
        listing_row,
        exclude=None if own is None else own.id,
        thresholds=thresholds,
        limit=max_candidates,
    )
    matches = _best_matches(session, listing_row, candidates, thresholds)
    group = _compatible(session, listing_row.id, own, candidates, matches)
    if not group:
        return GroupingOutcome(property=own)
    # A new listing that finds only removed ones brings the unit back.
    relisted = (
        own is None
        and listing_row.removed_at is None
        and not any(_has_active_listing(session, row.id, besides=[listing_row.id]) for row in group)
    )
    if own is not None:
        group.append(own)
    survivor = _survivor(session, group)
    merged = []
    for row in group:
        if row is survivor:
            continue
        pair = matches.get(row.id) or matches[survivor.id]
        merge(
            session,
            survivor,
            row,
            at=at,
            actor=Actor.SYSTEM,
            payload=pair.as_payload(),
            listing_id=listing_row.id,
        )
        merged.append(row.id)
    if own is None:
        listing_row.property_id = survivor.id
        _audit_in_place(
            session,
            survivor,
            actor=Actor.SYSTEM,
            at=at,
            detail="grouped",
            payload={"listing_id": listing_row.id, **matches[survivor.id].as_payload()},
        )
        if relisted:
            session.flush()
            relist_in(session, survivor, listing_row, at=at)
    after_grouping(session, survivor, at=at, merged=bool(merged))
    return GroupingOutcome(property=survivor, merged=tuple(merged))


def merge(
    session: Session,
    survivor: Property,
    absorbed: Property,
    *,
    at: datetime,
    actor: Actor,
    payload: Mapping[str, Any],
    listing_id: int | None = None,
    detail: str = "merged",
) -> None:
    """Merge ``absorbed`` into ``survivor``: see the module docstring.

    ``payload`` (why) goes into both audit events, with ``listing_id`` (the listing
    that brought them together) when there is one.
    """
    from furnest.db.property_store import _audit_in_place

    for model in (Listing, Note, Email):
        session.execute(
            update(model).where(model.property_id == absorbed.id).values(property_id=survivor.id)
        )
    session.execute(
        update(Property)
        .where(Property.merged_into_id == absorbed.id)
        .values(merged_into_id=survivor.id)
    )
    absorbed.merged_into_id = survivor.id
    absorbed.updated_at = at
    if absorbed.score is not None and (survivor.score is None or absorbed.score > survivor.score):
        survivor.score = absorbed.score
        survivor.match_result = absorbed.match_result
        survivor.criteria_version_id = absorbed.criteria_version_id
        survivor.match_listing_id = absorbed.match_listing_id
    if survivor.address is None:
        survivor.address = absorbed.address
    if survivor.lat is None or survivor.lng is None:
        survivor.lat, survivor.lng = absorbed.lat, absorbed.lng
    if survivor.owner_type_override is None and absorbed.owner_type_override is not None:
        # What a person said about the unit outlives the row they said it on.
        survivor.owner_type_override = absorbed.owner_type_override
    if survivor.discord_message_id is None and absorbed.discord_message_id is not None:
        # The survivor takes over the message, so the one post shows the unit.
        survivor.discord_channel_id = absorbed.discord_channel_id
        survivor.discord_message_id = absorbed.discord_message_id
        absorbed.discord_channel_id = None
        absorbed.discord_message_id = None
    survivor.updated_at = at
    session.flush()
    why = dict(payload) if listing_id is None else {"listing_id": listing_id, **payload}
    _audit_in_place(
        session,
        absorbed,
        actor=actor,
        at=at,
        detail=detail,
        payload={"merged_into": survivor.id, **why},
    )
    _audit_in_place(
        session,
        survivor,
        actor=actor,
        at=at,
        detail=detail,
        payload={
            "merged_from": absorbed.id,
            "merged_status": absorbed.status.value,
            **why,
        },
    )


def after_grouping(session: Session, survivor: Property, *, at: datetime, merged: bool) -> None:
    """Bring the survivor in step with the listings it now has."""
    from furnest.db.property_store import (
        _apply_transition,
        _enqueue_notify,
        _manual_contact,
        _status_before_unavailable,
        _sync_message,
        _sync_owner_type,
        primary_listing,
    )

    session.flush()
    primary = primary_listing(session, survivor.id)
    if primary is not None:
        manual = _manual_contact(primary)
        if manual != survivor.manual_contact_required:
            survivor.manual_contact_required = manual
            _sync_message(session, survivor)
        _sync_owner_type(session, survivor, primary)
    active = primary is not None and primary.removed_at is None
    if survivor.status is PropertyStatus.UNAVAILABLE and active:
        restore_to = _status_before_unavailable(session, survivor.id)
        if restore_to is not None:
            _apply_transition(
                session, survivor, restore_to, actor=Actor.SYSTEM, at=at, detail="relisted"
            )
    if (
        merged
        and survivor.status is PropertyStatus.SUGGESTED
        and survivor.discord_message_id is None
    ):
        # A merged-away property's pending notification is skipped; post the survivor.
        _enqueue_notify(session, survivor.id)


def audit_property_ids(session: Session, property_id: int) -> list[int]:
    """The property and every property merged into it: whose audit events it shows."""
    return [property_id, *merged_ids(session, property_id)]


__all__ = [
    "GroupingOutcome",
    "after_grouping",
    "audit_property_ids",
    "group_listing",
    "listing_ids_of",
    "merge",
    "merged_ids",
    "resolve",
    "separate",
    "separations",
    "set_address_keys",
    "unseparate",
]
