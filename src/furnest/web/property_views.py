"""What the dashboard's property views show, gathered from the property store.

The dashboard groups the fourteen statuses into a handful of tabs (:data:`TABS`):
"in progress" covers every step between drafting an inquiry and applying, and
"closed" covers won and lost. ``all`` is everything still in play, so archived
properties only show up on their own tab.

The list view pages through one tab. The board view shows every tab but ``all``
and ``archived`` as a column, each capped at :data:`PAGE_SIZE` cards with a link
to the full list. Search, sort and the "include snoozed" switch apply to both.

Status actions: the detail page offers a button for each move the state machine
allows the dashboard (:func:`status_actions`), and the list view can deny or archive
several properties at once (:func:`apply_bulk`). A bulk action moves each property in
its own transaction and skips, with the reason, any it may not move.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import ceil
from urllib.parse import urlencode

from sqlalchemy.orm import Session, sessionmaker

from furnest.db.property_store import (
    AuditEntry,
    PropertyNotFoundError,
    PropertyRow,
    PropertySort,
    SqlPropertyStore,
)
from furnest.domain import Actor, InvalidTransitionError, PropertyStatus, allowed_targets

S = PropertyStatus

PAGE_SIZE = 25


class View(StrEnum):
    LIST = "list"
    BOARD = "board"


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    statuses: frozenset[PropertyStatus]
    on_board: bool = True


TABS: tuple[Tab, ...] = (
    Tab("suggested", "Suggested", frozenset({S.SUGGESTED})),
    Tab("new", "New", frozenset({S.NEW})),
    Tab("approved", "Approved", frozenset({S.APPROVED})),
    Tab(
        "in_progress",
        "In progress",
        frozenset(
            {S.INQUIRY_DRAFTED, S.INQUIRY_SENT, S.IN_PROGRESS, S.RESPONDED, S.TOURING, S.APPLIED}
        ),
    ),
    Tab("closed", "Won / lost", frozenset({S.WON, S.LOST})),
    Tab("denied", "Denied", frozenset({S.DENIED})),
    Tab("unavailable", "Unavailable", frozenset({S.UNAVAILABLE})),
    Tab("archived", "Archived", frozenset({S.ARCHIVED}), on_board=False),
    Tab("all", "All", frozenset(set(S) - {S.ARCHIVED}), on_board=False),
)
TAB_KEYS = tuple(tab.key for tab in TABS)
DEFAULT_TAB = "suggested"
_BY_KEY = {tab.key: tab for tab in TABS}

SORT_LABELS = {
    PropertySort.SCORE: "Best score",
    PropertySort.PRICE: "Lowest price",
    PropertySort.NEWEST: "Newest",
}


@dataclass(frozen=True)
class Filters:
    """The dashboard's query string, validated."""

    status: str = DEFAULT_TAB
    q: str = ""
    sort: PropertySort = PropertySort.SCORE
    view: View = View.LIST
    page: int = 1
    snoozed: bool = False

    @property
    def tab(self) -> Tab:
        return _BY_KEY[self.status]

    def url(self, **overrides: object) -> str:
        """``/`` with these filters and ``overrides``, leaving defaults out."""
        values = {
            "status": self.status,
            "q": self.q,
            "sort": self.sort.value,
            "view": self.view.value,
            "page": self.page,
            "snoozed": self.snoozed,
            **overrides,
        }
        defaults = Filters()
        params = {}
        for name, value in values.items():
            default = getattr(defaults, name)
            if value == default or (isinstance(default, StrEnum) and value == default.value):
                continue
            params[name] = "true" if value is True else str(value)
        query = urlencode(params)
        return f"/?{query}" if query else "/"


@dataclass(frozen=True)
class TabCount:
    tab: Tab
    count: int


@dataclass(frozen=True)
class Column:
    tab: Tab
    rows: list[PropertyRow]
    total: int


@dataclass(frozen=True)
class Dashboard:
    filters: Filters
    tabs: list[TabCount]
    # List view: the page shown. Board view: empty.
    rows: list[PropertyRow]
    total: int
    page: int
    pages: int
    # Board view: one per board tab. List view: empty.
    columns: list[Column]


def build_dashboard(
    session_factory: sessionmaker[Session], filters: Filters, *, now: datetime
) -> Dashboard:
    store = SqlPropertyStore(session_factory)
    text = filters.q or None
    counts = store.status_counts(now=now, text=text, include_snoozed=filters.snoozed)
    tabs = [TabCount(tab, sum(counts.get(s, 0) for s in tab.statuses)) for tab in TABS]

    def search(tab: Tab, offset: int) -> tuple[list[PropertyRow], int]:
        page = store.search(
            now=now,
            limit=PAGE_SIZE,
            offset=offset,
            statuses=tab.statuses,
            text=text,
            sort=filters.sort,
            include_snoozed=filters.snoozed,
        )
        return page.rows, page.total

    if filters.view is View.BOARD:
        columns = []
        for tab in TABS:
            if tab.on_board:
                rows, total = search(tab, 0)
                columns.append(Column(tab, rows, total))
        return Dashboard(filters, tabs, [], 0, 1, 1, columns)

    total = next(count.count for count in tabs if count.tab == filters.tab)
    pages = max(1, ceil(total / PAGE_SIZE))
    page = min(filters.page, pages)
    rows, total = search(filters.tab, (page - 1) * PAGE_SIZE)
    pages = max(1, ceil(total / PAGE_SIZE))
    return Dashboard(filters, tabs, rows, total, page, pages, [])


def beds_baths(beds: int | None, baths: float | None) -> str | None:
    parts = []
    if beds is not None:
        parts.append("Studio" if beds == 0 else f"{beds} bd")
    if baths is not None:
        parts.append(f"{baths:g} ba")
    return " · ".join(parts) or None


def status_label(status: PropertyStatus) -> str:
    return status.value.replace("_", " ")


def audit_label(entry: AuditEntry) -> str:
    """One line for the detail page's timeline."""
    if entry.from_status is None:
        return f"Created as {status_label(entry.to_status)}"
    if entry.from_status == entry.to_status:
        payload = entry.payload or {}
        if payload.get("unsnoozed"):
            return "Snooze ended"
        if "snoozed_until" in payload:
            return f"Snoozed until {str(payload['snoozed_until'])[:10]}"
        return f"Updated while {status_label(entry.to_status)}"
    label = f"{status_label(entry.from_status)} → {status_label(entry.to_status)}"
    if entry.payload and "note_id" in entry.payload:
        label += " (reason in notes)"
    return label


ACTION_LABELS: dict[PropertyStatus, str] = {
    S.NEW: "Back to new",
    S.SUGGESTED: "Suggest",
    S.APPROVED: "Approve",
    S.DENIED: "Deny",
    S.INQUIRY_DRAFTED: "Inquiry drafted",
    S.INQUIRY_SENT: "Inquiry sent",
    S.IN_PROGRESS: "In progress",
    S.RESPONDED: "Responded",
    S.TOURING: "Touring",
    S.APPLIED: "Applied",
    S.WON: "Won",
    S.LOST: "Lost",
    S.ARCHIVED: "Archive",
    S.UNAVAILABLE: "Unavailable",
}


def status_actions(status: PropertyStatus) -> list[tuple[PropertyStatus, str]]:
    """The detail page's status buttons: every move the dashboard may make.

    Deny is left out; it has its own form, with an optional reason.
    """
    return [
        (to, ACTION_LABELS[to])
        for to in allowed_targets(status, Actor.DASHBOARD)
        if to is not S.DENIED
    ]


class BulkAction(StrEnum):
    DENY = "deny"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class BulkResult:
    action: BulkAction
    moved: list[int]
    # (property id, why it was not moved)
    skipped: list[tuple[int, str]]

    def notice(self) -> str:
        verb = "Denied" if self.action is BulkAction.DENY else "Archived"
        text = f"{verb} {len(self.moved)}."
        if self.skipped:
            reasons = "; ".join(f"#{pid}: {why}" for pid, why in self.skipped)
            text += f" Skipped {len(self.skipped)} ({reasons})."
        return text


def apply_bulk(
    store: SqlPropertyStore, ids: list[int], action: BulkAction, *, at: datetime
) -> BulkResult:
    """Deny or archive each property, as the dashboard, skipping those it can't move."""
    moved: list[int] = []
    skipped: list[tuple[int, str]] = []
    for property_id in dict.fromkeys(ids):
        try:
            if action is BulkAction.DENY:
                store.deny(property_id, actor=Actor.DASHBOARD, at=at)
            else:
                store.transition(property_id, S.ARCHIVED, actor=Actor.DASHBOARD, at=at)
        except PropertyNotFoundError:
            skipped.append((property_id, "not found"))
        except InvalidTransitionError as e:
            skipped.append((property_id, f"{status_label(e.current)}: {e.reason}"))
        else:
            moved.append(property_id)
    return BulkResult(action, moved, skipped)
