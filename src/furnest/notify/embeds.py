"""Discord messages for matched properties: a card per property, and the list that
announces a batch of them.

**Card.**
:func:`match_message` turns a :class:`~furnest.db.property_store.PropertyDetail` (the
same read model the dashboard's detail page uses) into the JSON payload that
:class:`~furnest.notify.discord.DiscordClient` posts or edits in. It is a pure
function, so the same property always renders the same payload, and the payload is
what the tests snapshot.

The embed shows the first photo, the monthly price, beds and baths, the score with
its top contributors, who is behind the unit when that is known (``owner_type``),
the source listing (the title links to it) and the dashboard's detail page. A listing
with no email address
(``manual_contact_required``) adds a Contact field that links to the source, where
the lister has to be messaged. Under it, one row of buttons: Approve, Deny, Inquire and
Snooze, each with a ``custom_id`` from :mod:`furnest.notify.custom_ids`.

**List.** New suggestions are announced together (:mod:`furnest.notify.post`):
:func:`batch_message` lists them, one line each with score, price and rooms, under
a menu (``furnest:open``) of the same properties. Picking one answers with that
property's card, buttons included, shown only to the person who picked it
(:mod:`furnest.web.discord_actions`). As decisions are made the list is redrawn
(:mod:`furnest.notify.sync`) with each property's status in front of its line.

Two things keep scraped text from misbehaving in a channel:

* ``allowed_mentions`` is empty, so an ``@everyone`` in a listing title pings nobody;
* text from a listing that lands in markdown (the description, link labels) has
  Discord's markdown characters escaped, and URLs inside ``[label](url)`` have their
  parentheses percent-encoded.

Every string is cut to Discord's limits (:data:`LIMITS`) with an ellipsis.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import Any

from furnest.db.base import utcnow
from furnest.db.property_store import FINISHED, PropertyDetail, PropertyState
from furnest.domain import (
    Actor,
    OwnerType,
    PropertyStatus,
    allowed_targets,
    beds_baths,
    owner_type_label,
)
from furnest.notify.custom_ids import OPEN_CUSTOM_ID, DiscordAction, encode_custom_id

JsonObject = dict[str, Any]

# Brand blue (--furnest-blue in app.css) as the embed's side bar.
EMBED_COLOR = 0x2563EB
# How many score contributors the embed lists.
TOP_BREAKDOWN_ITEMS = 3

# Discord's documented embed limits, in characters.
LIMITS = {
    "title": 256,
    "description": 4096,
    "field_name": 256,
    "field_value": 1024,
    "footer": 2048,
    "total": 6000,
}
# The address line is short; a longer one is surely scraped junk.
_MAX_ADDRESS = 300

# Component types and button styles from Discord's API.
ACTION_ROW = 1
BUTTON = 2
STRING_SELECT = 3
PRIMARY, SECONDARY, SUCCESS, DANGER = 1, 2, 3, 4

# Label and style of each button, in display order.
BUTTONS: dict[DiscordAction, tuple[str, int]] = {
    DiscordAction.APPROVE: ("Approve", SUCCESS),
    DiscordAction.DENY: ("Deny", DANGER),
    DiscordAction.INQUIRE: ("Inquire", PRIMARY),
    DiscordAction.SNOOZE: ("Snooze", SECONDARY),
}

# Statuses the Approve button acts on.
APPROVABLE = frozenset({PropertyStatus.SUGGESTED, PropertyStatus.DENIED})
# Statuses the Inquire button acts on.
INQUIRABLE = frozenset({PropertyStatus.SUGGESTED, PropertyStatus.APPROVED})

# A select menu takes at most this many options, so a list message shows at most
# this many properties.
MAX_BATCH = 25
# Discord's caps on a select option's label, description and value, in characters.
_MAX_OPTION_TEXT = 100
# A list line's title is cut to this, so 25 lines fit in one embed description.
_MAX_LINE_TITLE = 70

# The mark in front of a list line, by what became of the property. A property
# still waiting on a decision gets a plain bullet.
_DECIDED = "✅"
_DROPPED = "❌"
_SNOOZED = "💤"
_MOVED = "↪"
_DROPPED_STATUSES = frozenset(
    {
        PropertyStatus.DENIED,
        PropertyStatus.LOST,
        PropertyStatus.ARCHIVED,
        PropertyStatus.UNAVAILABLE,
    }
)

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#])")


def match_message(
    detail: PropertyDetail,
    *,
    base_url: str,
    source_name: Callable[[str], str] = str,
    disabled: Collection[DiscordAction] = (),
    superseded: bool = False,
) -> JsonObject:
    """The create/edit-message payload for ``detail``.

    ``base_url`` is the dashboard's public URL (``BASE_URL``). ``source_name`` turns an
    adapter key into its display name. Buttons in ``disabled`` are shown greyed out.
    ``superseded`` marks a message that a newer post of the same property replaced.
    """
    embed = match_embed(detail, base_url=base_url, source_name=source_name, superseded=superseded)
    return {
        "embeds": [embed],
        "components": [action_row(detail.property.id, disabled=disabled)],
        "allowed_mentions": {"parse": []},
    }


def batch_message(
    details: Sequence[PropertyDetail],
    *,
    base_url: str,
    superseded: Collection[int] = (),
    now: datetime | None = None,
) -> JsonObject:
    """The create/edit-message payload listing ``details`` (at most :data:`MAX_BATCH`),
    in the order given.

    ``superseded`` are the ids of properties posted again in a later list since.
    ``now`` decides whether a snooze is still running (default: the current time).
    """
    if len(details) > MAX_BATCH:
        msg = f"a list message shows at most {MAX_BATCH} properties, got {len(details)}"
        raise ValueError(msg)
    moment = now or utcnow()
    lines = [
        _batch_line(
            detail, base_url=base_url, superseded=detail.property.id in superseded, now=moment
        )
        for detail in details
    ]
    description = "\n".join(lines)
    if len(description) > LIMITS["description"]:
        # Long titles and many lines: drop the links rather than lose whole lines.
        lines = [
            _batch_line(
                detail,
                base_url=None,
                superseded=detail.property.id in superseded,
                now=moment,
            )
            for detail in details
        ]
        description = "\n".join(lines)
    count = len(details)
    embed: JsonObject = {
        "title": f"{count} new match{'' if count == 1 else 'es'}",
        "url": base_url.rstrip("/") + "/",
        "description": truncate(description, "description"),
        "color": EMBED_COLOR,
        "footer": {"text": "furnest · pick a listing below to approve, deny, inquire or snooze"},
    }
    payload: JsonObject = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if details:
        payload["components"] = [
            {
                "type": ACTION_ROW,
                "components": [
                    {
                        "type": STRING_SELECT,
                        "custom_id": OPEN_CUSTOM_ID,
                        "placeholder": "Open a listing to act on it",
                        "min_values": 1,
                        "max_values": 1,
                        "options": [_batch_option(detail) for detail in details],
                    }
                ],
            }
        ]
    return payload


def _batch_line(
    detail: PropertyDetail, *, base_url: str | None, superseded: bool, now: datetime
) -> str:
    """``• [Title](dashboard) · 87/100 · $1,800/mo · 2 bd / 1 ba`` for one property,
    with its mark and, once it isn't waiting any more, why."""
    state = detail.property
    title = escape_markdown(truncate(_title(detail), _MAX_LINE_TITLE))
    parts = [f"[{title}]({_link_url(dashboard_url(base_url, state.id))})" if base_url else title]
    parts.extend(_facts(detail))
    mark, note = "•", None
    if superseded or state.merged_into_id is not None:
        mark = _MOVED
        note = "reposted" if superseded else f"merged into #{state.merged_into_id}"
    elif state.status in _DROPPED_STATUSES:
        mark, note = _DROPPED, _status_text(state.status)
    elif state.status not in (PropertyStatus.NEW, PropertyStatus.SUGGESTED):
        mark, note = _DECIDED, _status_text(state.status)
    elif state.snoozed_until is not None and state.snoozed_until > now:
        mark, note = _SNOOZED, "snoozed"
    if note is not None:
        parts.append(f"*{note}*")
    return f"{mark} " + " · ".join(parts)


def _batch_option(detail: PropertyDetail) -> JsonObject:
    option: JsonObject = {
        "label": truncate(_title(detail), _MAX_OPTION_TEXT),
        "value": str(detail.property.id),
    }
    facts = _facts(detail)
    if facts:
        option["description"] = truncate(" · ".join(facts), _MAX_OPTION_TEXT)
    return option


def _title(detail: PropertyDetail) -> str:
    primary, state = detail.primary, detail.property
    if primary is not None and primary.summary.title:
        return primary.summary.title
    return state.address or f"Property #{state.id}"


def _facts(detail: PropertyDetail) -> list[str]:
    """Score, price and rooms, whichever are known."""
    facts: list[str] = []
    if detail.property.score is not None:
        facts.append(f"{detail.property.score}/100")
    primary = detail.primary
    if primary is not None:
        if primary.summary.monthly_price is not None:
            facts.append(f"{primary.summary.monthly_price.format()}/mo")
        rooms = beds_baths(primary.summary.beds, primary.summary.baths)
        if rooms is not None:
            facts.append(rooms)
    return facts


def match_embed(
    detail: PropertyDetail,
    *,
    base_url: str,
    source_name: Callable[[str], str] = str,
    superseded: bool = False,
) -> JsonObject:
    state = detail.property
    primary = detail.primary
    embed: JsonObject = {
        "title": truncate(primary.summary.title if primary else f"Property #{state.id}", "title"),
        "color": EMBED_COLOR,
    }
    if primary is not None:
        embed["url"] = str(primary.summary.url)
    if state.address:
        embed["description"] = escape_markdown(truncate(state.address, _MAX_ADDRESS))
    if detail.images:
        embed["image"] = {"url": detail.images[0]}

    fields: list[JsonObject] = []
    price = primary.summary.monthly_price if primary else None
    if price is not None:
        fields.append(_field("Price", f"{price.format()}/mo", inline=True))
    rooms = beds_baths(primary.summary.beds, primary.summary.baths) if primary else None
    if rooms is not None:
        fields.append(_field("Beds / Baths", rooms, inline=True))
    if state.score is not None:
        fields.append(_field("Score", f"{state.score}/100", inline=True))
    if state.owner_type is not OwnerType.UNKNOWN:
        fields.append(_field("Owner", owner_type_label(state.owner_type), inline=True))
    reasons = _top_breakdown(detail)
    if reasons:
        fields.append(_field("Why it matched", reasons))
    if state.manual_contact_required and primary is not None:
        source = escape_markdown(source_name(primary.summary.source))
        url = _link_url(str(primary.summary.url))
        fields.append(
            _field("Contact", f"No email address. Message the lister on [{source}]({url}).")
        )
    links = [f"[Open in furnest]({_link_url(dashboard_url(base_url, state.id))})"]
    if primary is not None:
        label = escape_markdown(source_name(primary.summary.source))
        links.insert(0, f"[{label}]({_link_url(str(primary.summary.url))})")
    fields.append(_field("Links", " · ".join(links)))
    embed["fields"] = fields

    footer = f"furnest · {_status_text(state.status)}"
    if state.snoozed_until is not None:
        footer += f" · snoozed until {state.snoozed_until:%Y-%m-%d}"
    if superseded:
        footer += " · reposted"
    if state.merged_into_id is not None:
        footer += f" · merged into #{state.merged_into_id}"
    embed["footer"] = {"text": truncate(footer, "footer")}
    embed["timestamp"] = state.status_changed_at.isoformat()
    return embed


def action_row(property_id: int, *, disabled: Collection[DiscordAction] = ()) -> JsonObject:
    """The Approve / Deny / Inquire / Snooze buttons for one property."""
    return {
        "type": ACTION_ROW,
        "components": [
            {
                "type": BUTTON,
                "style": style,
                "label": label,
                "custom_id": encode_custom_id(action, property_id),
                "disabled": action in disabled,
            }
            for action, (label, style) in BUTTONS.items()
        ],
    }


def disabled_actions(state: PropertyState) -> frozenset[DiscordAction]:
    """The buttons that would do nothing for a property in ``state``'s status.

    Approve works on a suggestion or a denied property. (The state machine also lets
    a drafted inquiry go back to ``approved``, but a stale click mustn't throw a draft
    away.) Deny follows the state machine for the Discord actor. Inquire works from
    ``suggested`` (it approves first) and ``approved``. Snooze works until the
    property is finished.
    """
    allowed = allowed_targets(state.status, Actor.DISCORD)
    enabled = {
        DiscordAction.APPROVE: state.status in APPROVABLE,
        DiscordAction.DENY: PropertyStatus.DENIED in allowed,
        DiscordAction.INQUIRE: state.status in INQUIRABLE,
        DiscordAction.SNOOZE: state.status not in FINISHED,
    }
    return frozenset(action for action, on in enabled.items() if not on)


def dashboard_url(base_url: str, property_id: int) -> str:
    """The property's detail page on the dashboard."""
    return f"{base_url.rstrip('/')}/properties/{property_id}"


def escape_markdown(text: str) -> str:
    """``text`` with Discord's markdown characters backslash-escaped, so it shows as typed."""
    return _MARKDOWN.sub(r"\\\1", text)


def truncate(text: str, limit: str | int) -> str:
    """``text`` cut to ``limit`` characters (a :data:`LIMITS` key or a number), ending in "…"."""
    size = LIMITS[limit] if isinstance(limit, str) else limit
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def embed_length(embed: JsonObject) -> int:
    """Characters that count towards Discord's 6000 per-embed total."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        total += len(field["name"]) + len(field["value"])
    return total


def _field(name: str, value: str, *, inline: bool = False) -> JsonObject:
    return {
        "name": truncate(name, "field_name"),
        "value": truncate(value, "field_value"),
        "inline": inline,
    }


def _top_breakdown(detail: PropertyDetail) -> str | None:
    """The score's biggest contributors, one per line, e.g. "Price fit — 27/30 pts"."""
    if detail.match is None:
        return None
    items = sorted(
        (item for item in detail.match.breakdown if item.points > 0),
        key=lambda item: item.points,
        reverse=True,
    )[:TOP_BREAKDOWN_ITEMS]
    lines = [
        f"• {escape_markdown(item.label)} — {_points(item.points)}/{_points(item.max_points)} pts"
        for item in items
    ]
    return "\n".join(lines) or None


def _points(value: float) -> str:
    return f"{round(value, 1):g}"


def _status_text(status: PropertyStatus) -> str:
    return status.value.replace("_", " ")


def _link_url(url: str) -> str:
    """``url`` made safe inside ``[label](url)``: a ")" would end the link early."""
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
