"""Lenient readers for provider JSON, shared by the adapters' item mappings.

Data providers return loosely typed JSON: numbers as strings, ``true`` where a count
belongs, ``-1`` or ``0`` for "unknown". Each reader returns ``None`` (or ``""``) for
anything it can't use instead of raising, so one odd field doesn't drop a listing.
"""

from __future__ import annotations

import math


def text(value: object) -> str:
    """``value`` stripped if it's a string, else ``""``."""
    return value.strip() if isinstance(value, str) else ""


def number(value: object) -> float | None:
    """A finite number from an int, float or numeric string. Booleans don't count."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        found = float(value)
    except ValueError:
        return None
    return found if math.isfinite(found) else None


def whole(value: object) -> int | None:
    """A non-negative count, truncated."""
    found = number(value)
    return int(found) if found is not None and found >= 0 else None


def half(value: object) -> float | None:
    """A non-negative count to the nearest half (bathrooms)."""
    found = number(value)
    return round(found * 2) / 2 if found is not None and found >= 0 else None


def positive(value: object) -> int | None:
    """A count where 0 means unknown (square footage)."""
    found = whole(value)
    return found if found else None
