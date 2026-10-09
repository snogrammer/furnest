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

Submitted rows are renumbered in the order they are kept, so the form re-rendered
after a failed save and the error keys (e.g. ``areas-1-lat``) use the same indices.

:func:`parse_form` never raises. It returns what the user typed (to re-render), the
validated :class:`Criteria` when everything is valid, and per-field error messages
otherwise, keyed by input name, with ``__all__`` for errors about the whole profile.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.datastructures import FormData

from furnest.domain import DEFAULT_CURRENCY, Criteria, DistanceUnit, Money, get_currency

GENERAL = "__all__"

SCALARS = (
    "min_beds",
    "min_baths",
    "min_sqft",
    "move_in_earliest",
    "move_in_latest",
    "lease_min_months",
    "lease_max_months",
    "price_fit_weight",
    "extra_beds_weight",
    "my_move_in_date",
    "my_lease_months",
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


def _read_state(form: FormData) -> FormState:
    fields = {name: _text(form, name) for name in (*SCALARS, *PRICES, *TEXT_LISTS)}
    fields["currency"] = _text(form, "currency").upper() or DEFAULT_CURRENCY
    checked = {name: set(_strings(form.getlist(name))) for name in CHECK_LISTS}
    if "furnished_required" in form:
        checked["furnished_required"] = {"on"}

    areas = []
    for i in _row_indices(form, "areas"):
        row = {name: _text(form, f"areas-{i}-{name}") for name in AREA_FIELDS}
        row["unit"] = _text(form, f"areas-{i}-unit") or DistanceUnit.MILES.value
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


def _split(text: str) -> list[str]:
    return [part.strip() for part in _SPLIT.split(text) if part.strip()]


def _to_data(state: FormState, errors: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {name: state.fields[name] or None for name in SCALARS}

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
    state = _read_state(form)
    errors: dict[str, str] = {}
    data = _to_data(state, errors)
    try:
        criteria = Criteria.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            errors.setdefault(_error_key(error["loc"], state), _message(error["msg"]))
        return ParsedForm(state, None, errors)
    if errors:
        return ParsedForm(state, None, errors)
    return ParsedForm(state, criteria, {})
