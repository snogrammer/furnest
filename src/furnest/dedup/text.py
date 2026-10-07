"""Fuzzy similarity of listing titles and descriptions (rapidfuzz).

Sources reword and reorder the same ad: "Furnished 2BR near downtown, parking" on
one is "2 bed furnished apt - downtown - free parking" on another. Token-set
similarity ignores word order and words only one side has, so it scores those two
close while two different ads for the same neighborhood stay apart.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Longer descriptions are cut to this many characters before comparing: the opening
# carries the ad, and the comparison cost grows with the length.
MAX_TEXT_LENGTH = 2000

_NON_WORD = re.compile(r"[^\w]+")


def normalize_text(text: str | None) -> str:
    """Lower-case words separated by single spaces; punctuation dropped."""
    if not text:
        return ""
    return _NON_WORD.sub(" ", text[:MAX_TEXT_LENGTH].lower()).strip()


def text_similarity(a: str | None, b: str | None) -> float | None:
    """How alike two texts are, from 0 to 1, or ``None`` when either is empty."""
    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return None
    return fuzz.token_set_ratio(left, right) / 100
