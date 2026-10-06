"""The Discord message for a matched property: one embed plus a row of action buttons.

:func:`match_message` turns a :class:`~furnest.db.property_store.PropertyDetail` (the
same read model the dashboard's detail page uses) into the JSON payload that
:class:`~furnest.notify.discord.DiscordClient` posts or edits in. It is a pure
function, so the same property always renders the same payload, and the payload is
what the tests snapshot.

The embed shows the first photo, the monthly price, beds and baths, the score with
its top contributors, the source listing (the title links to it) and the
dashboard's detail page. Under it, one row of buttons: Approve, Deny, Inquire and
Snooze, each with a ``custom_id`` from :mod:`furnest.notify.custom_ids`.

Two things keep scraped text from misbehaving in a channel:

* ``allowed_mentions`` is empty, so an ``@everyone`` in a listing title pings nobody;
* text from a listing that lands in markdown (the description, link labels) has
  Discord's markdown characters escaped, and URLs inside ``[label](url)`` have their
  parentheses percent-encoded.

Every string is cut to Discord's limits (:data:`LIMITS`) with an ellipsis.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from typing import Any

from furnest.db.property_store import FINISHED, PropertyDetail, PropertyState
from furnest.domain import Actor, PropertyStatus, allowed_targets, beds_baths
from furnest.notify.custom_ids import DiscordAction, encode_custom_id

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
    reasons = _top_breakdown(detail)
    if reasons:
        fields.append(_field("Why it matched", reasons))
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
