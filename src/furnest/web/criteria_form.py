"""The criteria editor form: flat HTML form fields to and from :class:`Criteria`.

A browser form can only send flat key/value pairs, so the editor uses a few
conventions:

- Scalars (beds, dates, weights) are plain inputs. Blank means "not set".
- Prices are typed in major units ("1500") with one ``currency`` input for both.
- ZIP codes, neighborhoods and keywords are textareas: one entry per line or comma.
- Property types, denied owner types, must-have amenities and ``unknown_fails`` are
  checkbox groups.
- Areas and nice-to-have weights are repeated rows named ``areas-<i>-<field>`` and
  ``nice-<i>-<field>``. A row left blank, or with its ``remove`` box ticked, is dropped.
- An area row's name is a "City, State" lookup (#179). A row with a name and no
  coordinates is filled from :mod:`furnest.domain.places`: the canonical name, the
  centre, and a 50 mi radius unless one was typed. ``lookup=<i>`` (the row's "Look up"
  button) clears that row's coordinates first, so a renamed area is re-pointed, and
  marks the parse as a lookup the caller shows instead of saving.

Submitted rows are renumbered in the order they are kept, so the form re-rendered
after a failed save and the error keys (e.g. ``areas-1-lat``) use the same indices.

:func:`parse_form` never raises. It returns what the user typed (to re-render), the
validated :class:`Criteria` when everything is valid, and per-field error messages
otherwise, keyed by input name, with ``__all__`` for errors about the whole profile.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.datastructures import FormData

from furnest.domain import (
    DEFAULT_CURRENCY,
    Criteria,
    DistanceUnit,
    Money,
    Place,
    find_places,
    get_currency,
    neighborhood_names,
    resolve_place,
)

GENERAL = "__all__"

SCALARS = (
    "min_beds",
    "min_baths",
    "min_sqft",
    "move_in",
    "move_in_flex_days",
    "move_out",
    "move_out_flex_days",
    "price_fit_weight",
    "extra_beds_weight",
)
PRICES = ("price_min", "price_max")
TEXT_LISTS = ("zip_codes", "neighborhoods", "keywords_allow", "keywords_deny")
CHECK_LISTS = (
    "property_types_allow",
    "property_types_deny",
    "owner_types_deny",
    "must_have_amenities",
    "unknown_fails",
)
AREA_FIELDS = ("name", "lat", "lng", "radius")
DEFAULT_AREA_RADIUS = "50"
DEFAULT_AREA_UNIT = DistanceUnit.MILES

_ROW_KEY = re.compile(r"^(areas|nice)-(\d+)-")
_SPLIT = re.compile(r"[,\n]")


@dataclass
class FormState:
    """What the form shows: text exactly as typed (or as stored), ready to render."""

    fields: dict[str, str] = field(default_factory=dict)
    checked: dict[str, set[str]] = field(default_factory=dict)
    areas: list[dict[str, str]] = field(default_factory=list)
    nice: list[tuple[str, str]] = field(default_factory=list)

    def value(self, name: str) -> str:
        return self.fields.get(name, "")

    def is_checked(self, group: str, value: str) -> bool:
        return value in self.checked.get(group, set())


@dataclass(frozen=True)
class ParsedForm:
    state: FormState
    criteria: Criteria | None
    errors: dict[str, str]
    # True when a button other than Save submitted the form: show it, don't store it.
    lookup: bool = False
    notice: str = ""


def _show(value: object) -> str:
    if value is None:
        return ""
    return f"{value:g}" if isinstance(value, float) else str(value)


def state_from_criteria(criteria: Criteria) -> FormState:
    """The form prefilled from a stored profile."""
    fields = {name: _show(getattr(criteria, name)) for name in SCALARS}
    currency = next(
        (p.currency for p in (criteria.price_min, criteria.price_max) if p is not None),
        DEFAULT_CURRENCY,
    )
    fields["currency"] = currency
    for name in PRICES:
        money: Money | None = getattr(criteria, name)
        fields[name] = "" if money is None else str(money.major)
    for name in TEXT_LISTS:
        fields[name] = "\n".join(getattr(criteria, name))
    checked = {name: {str(v) for v in getattr(criteria, name)} for name in CHECK_LISTS}
    if criteria.furnished_required:
        checked["furnished_required"] = {"on"}
    areas = [
        {
            "name": area.name,
            "lat": _show(area.lat),
            "lng": _show(area.lng),
            "radius": _show(area.radius),
            "unit": str(area.unit),
        }
        for area in criteria.areas
    ]
    nice = [(tag, _show(weight)) for tag, weight in criteria.nice_to_have_amenities.items()]
    return FormState(fields=fields, checked=checked, areas=areas, nice=nice)


def _text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _strings(values: Iterable[object]) -> list[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def _row_indices(form: FormData, prefix: str) -> list[int]:
    found = {
        int(match.group(2))
        for key in form
        if (match := _ROW_KEY.match(key)) and match.group(1) == prefix
    }
    return sorted(found)


def _read_state(form: FormData, lookup: int | None = None) -> FormState:
    fields = {name: _text(form, name) for name in (*SCALARS, *PRICES, *TEXT_LISTS)}
    fields["currency"] = _text(form, "currency").upper() or DEFAULT_CURRENCY
    checked = {name: set(_strings(form.getlist(name))) for name in CHECK_LISTS}
    if "furnished_required" in form:
        checked["furnished_required"] = {"on"}

    areas = []
    for i in _row_indices(form, "areas"):
        row = {name: _text(form, f"areas-{i}-{name}") for name in AREA_FIELDS}
        row["unit"] = _text(form, f"areas-{i}-unit") or DistanceUnit.MILES.value
        if i == lookup and row["name"]:
            row["lat"] = row["lng"] = ""
        if f"areas-{i}-remove" in form or not any(row[name] for name in AREA_FIELDS):
            continue
        areas.append(row)

    nice = []
    for i in _row_indices(form, "nice"):
        tag, weight = _text(form, f"nice-{i}-tag"), _text(form, f"nice-{i}-weight")
        if f"nice-{i}-remove" in form or not (tag or weight):
            continue
        nice.append((tag, weight))
    return FormState(fields=fields, checked=checked, areas=areas, nice=nice)


def resolve_area_row(row: dict[str, str]) -> str | None:
    """Fill a named row without coordinates from the places data, or say why not.

    The row is changed in place. A row that already has a coordinate is left alone, and
    so is a row that can't be resolved: it is shown again as typed, with the message.
    """
    name = row.get("name", "")
    if not name or row.get("lat") or row.get("lng"):
        return None
    place = resolve_place(name)
    if place is None:
        options = [p.label for p in find_places(name, limit=5)]
        if not options:
            return f'no US city matches {name!r}; try "City, ST"'
        return "pick one: " + "; ".join(options)
    row["name"], row["lat"], row["lng"] = place.label, _show(place.lat), _show(place.lng)
    if not row.get("radius"):
        row["radius"], row["unit"] = DEFAULT_AREA_RADIUS, DEFAULT_AREA_UNIT.value
    return None


def look_up_row(params: Mapping[str, str], index: int) -> tuple[dict[str, str], str | None]:
    """One area row as submitted (``areas-<index>-*``), re-resolved from its name."""
    row = {name: params.get(f"areas-{index}-{name}", "").strip() for name in AREA_FIELDS}
    row["unit"] = params.get(f"areas-{index}-unit", "").strip() or DistanceUnit.MILES.value
    if row["name"]:
        row["lat"] = row["lng"] = ""
    return row, resolve_area_row(row)


def lookup_index(form: FormData) -> int | None:
    """The area row whose "Look up" button submitted the form, if one did."""
    value = _text(form, "lookup")
    return int(value) if value.isdigit() else None


def merge_lines(existing: str, additions: Iterable[str]) -> tuple[str, int]:
    """``existing`` (a ZIP or neighborhood list) plus the new entries, one per line.

    Entries already there, ignoring case, are not added twice. Returns the text and how
    many entries were added.
    """
    entries = _split(existing)
    seen = {e.casefold() for e in entries}
    added = 0
    for item in additions:
        if item.casefold() not in seen:
            seen.add(item.casefold())
            entries.append(item)
            added += 1
    return "\n".join(entries), added


def place_of(row: Mapping[str, str]) -> Place | None:
    """The city an area row names, once it has been looked up (a typed name is not enough)."""
    name = row.get("name", "")
    place = resolve_place(name) if name and row.get("lat") and row.get("lng") else None
    return place if place is not None and place.label == name else None


def suggested_neighborhoods(place: Place, stored: Iterable[str], present: str) -> list[str]:
    """Dataset and stored-listing names for a city that the neighborhood list lacks."""
    have = {n.casefold() for n in _split(present)}
    seen: set[str] = set()
    names = []
    for name in (*stored, *neighborhood_names(place)):
        folded = name.casefold()
        if folded not in have and folded not in seen:
            seen.add(folded)
            names.append(name)
    return names


def _apply_hint_action(form: FormData, state: FormState, errors: dict[str, str]) -> str | None:
    """Handle the "Add ZIP codes" and neighborhood-chip buttons; the notice, if one fired."""
    zips_for = _text(form, "add_zips")
    if zips_for.isdigit():
        i = int(zips_for)
        place = resolve_place(_text(form, f"areas-{i}-name"))
        if place is None:
            errors[f"areas-{i}-name"] = 'pick a city first, as "City, ST"'
            return "Pick a city to add its ZIP codes."
        text, added = merge_lines(state.fields["zip_codes"], place.zip_codes)
        state.fields["zip_codes"] = text
        return f"Added {added} ZIP codes for {place.label}. Review and save."
    hood = _text(form, "add_neighborhood")
    if hood:
        state.fields["neighborhoods"], added = merge_lines(state.fields["neighborhoods"], [hood])
        return f"Added {hood}. Review and save." if added else f"{hood} is already listed."
    return None


def _split(text: str) -> list[str]:
    return [part.strip() for part in _SPLIT.split(text) if part.strip()]


def _to_data(state: FormState, errors: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {name: state.fields[name] or None for name in SCALARS}
    for name in ("move_in_flex_days", "move_out_flex_days"):
        if data[name] is None:
            del data[name]  # blank means the default

    currency = state.fields["currency"]
    try:
        currency = get_currency(currency).code
    except ValueError:
        errors["currency"] = f"unknown currency {currency!r}"
    for name in PRICES:
        text = state.fields[name].replace(",", "").lstrip("$")
        if not text:
            data[name] = None
        elif "currency" not in errors:
            try:
                money = Money.from_major(text, currency)
            except ValueError:
                errors[name] = "enter an amount such as 1500"
            else:
                data[name] = money

    for name in TEXT_LISTS:
        data[name] = _split(state.fields[name])
    for name in CHECK_LISTS:
        data[name] = sorted(state.checked.get(name, set()))
    data["furnished_required"] = "furnished_required" in state.checked

    for i, row in enumerate(state.areas):
        if message := resolve_area_row(row):
            errors[f"areas-{i}-name"] = message
    data["areas"] = [{k: v for k, v in row.items() if v} for row in state.areas]

    weights: dict[str, str] = {}
    for i, (tag, weight) in enumerate(state.nice):
        if not tag:
            errors[f"nice-{i}-tag"] = "choose an amenity"
        elif tag in weights:
            errors[f"nice-{i}-tag"] = "listed twice"
        elif not weight:
            errors[f"nice-{i}-weight"] = "enter a weight"
        else:
            weights[tag] = weight
    data["nice_to_have_amenities"] = weights
    return data


def _error_key(loc: tuple[int | str, ...], state: FormState) -> str:
    if not loc:
        return GENERAL
    head = str(loc[0])
    if head == "areas" and len(loc) >= 3:
        return f"areas-{loc[1]}-{loc[2]}"
    if head == "nice_to_have_amenities" and len(loc) >= 2:
        tags = [tag for tag, _ in state.nice]
        if loc[1] in tags:
            return f"nice-{tags.index(str(loc[1]))}-weight"
    return head


def _message(text: str) -> str:
    return text.removeprefix("Value error, ")


def parse_form(form: FormData) -> ParsedForm:
    """Read a submitted editor form into criteria, or into errors to show beside it."""
    lookup = lookup_index(form)
    state = _read_state(form, lookup)
    errors: dict[str, str] = {}
    notice = _apply_hint_action(form, state, errors)
    data = _to_data(state, errors)
    lookup_shown = lookup is not None or notice is not None
    notice = notice or ""
    try:
        criteria = Criteria.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            key = _error_key(error["loc"], state)
            # A city that wasn't found already explains the missing coordinates.
            if key.startswith("areas-") and key.rsplit("-", 1)[0] + "-name" in errors:
                continue
            errors.setdefault(key, _message(error["msg"]))
        return ParsedForm(state, None, errors, lookup_shown, notice)
    if errors:
        return ParsedForm(state, None, errors, lookup_shown, notice)
    return ParsedForm(state, criteria, {}, lookup_shown, notice)
