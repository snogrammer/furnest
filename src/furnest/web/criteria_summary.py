"""A plain-English line describing a :class:`Criteria`, for the editor and dashboard."""

from datetime import date

from furnest.domain import Criteria, Money
from furnest.domain.money import get_currency

EMPTY_SUMMARY = "No criteria saved yet: every listing is shown."


def _amount(money: Money) -> str:
    currency = get_currency(money.currency)
    major = money.major
    digits = 0 if major == major.to_integral_value() else currency.exponent
    return f"{currency.symbol}{major:,.{digits}f}"


def _day(value: date) -> str:
    return f"{value.day} {value:%b}"


def _price(criteria: Criteria) -> str | None:
    low, high = criteria.price_min, criteria.price_max
    if low and high:
        return f"{_amount(low)}\u2013{_amount(high)}/mo"
    if high:
        return f"up to {_amount(high)}/mo"
    if low:
        return f"from {_amount(low)}/mo"
    return None


def _where(criteria: Criteria) -> str | None:
    places = [f"within {a.radius:g} {a.unit.value} of {a.name}" for a in criteria.areas]
    if criteria.zip_codes:
        places.append(f"ZIP {', '.join(criteria.zip_codes)}")
    if criteria.neighborhoods:
        places.append(", ".join(criteria.neighborhoods))
    return " or ".join(places) or None


def summarize_criteria(criteria: Criteria) -> str:
    """e.g. "Furnished, 1+ bed, within 50 mi of Austin, TX, $1,500 to $2,500/mo, move in
    around 8 Nov ± 7 days, out around 15 May ± 90 days"."""
    parts: list[str] = []
    if criteria.furnished_required:
        parts.append("Furnished")
    if criteria.min_beds is not None:
        parts.append("studio or larger" if criteria.min_beds == 0 else f"{criteria.min_beds}+ bed")
    if criteria.min_baths:
        parts.append(f"{criteria.min_baths:g}+ bath")
    if criteria.min_sqft:
        parts.append(f"{criteria.min_sqft:,}+ sqft")
    parts += [p for p in (_where(criteria), _price(criteria)) if p]
    if criteria.move_in:
        parts.append(f"move in around {_day(criteria.move_in)} ± {criteria.move_in_flex_days} days")
    if criteria.move_out:
        parts.append(f"out around {_day(criteria.move_out)} ± {criteria.move_out_flex_days} days")
    if criteria.must_have_amenities:
        parts.append(f"must have {len(criteria.must_have_amenities)} amenities")
    if not parts:
        return EMPTY_SUMMARY
    text = ", ".join(parts)
    return text[0].upper() + text[1:]
