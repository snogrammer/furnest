"""KeywordAmenityExtractor: deterministic amenity tags from listing text and adapter labels.

Text is lower-cased and cut into clauses at sentence punctuation, commas and
contrast words ("but", "however"). Negation never crosses a clause, so in "No
smoking, pets welcome" the "no" stays with smoking. Each phrase hit from
:mod:`~furnest.matching.amenities.vocabulary` has a polarity: affirmative or
absent. The polarity flips when one of the three words before the hit is a negator
("no", "without", "isn't"…) or when the rest of the clause starts with one
("dishwasher not included").

Two sources are read separately: the free text (title and description) and the
adapter's own amenity labels. Within a source, "absent" beats "present" when they
disagree, which is the cautious answer for a must-have. Across sources the adapter
wins, because a structured field is more trustworthy than prose (the same rule
address parsing follows). Finally pet tags are reconciled: "cats ok" implies pets
are allowed, and "no pets" rules out cats and dogs unless they were stated.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from furnest.domain import Amenity, AmenityResult, NormalizedListing
from furnest.matching.amenities.vocabulary import ABSENT_FORMS, PET_KINDS, SYNONYMS

_CLAUSE = re.compile(r"[.,;:!?\n()]|\b(?:but|however|although|though|except)\b")
_TOKEN = re.compile(r"[a-z0-9']+")
_NEGATORS = frozenset({"no", "not", "without", "never", "none", "non", "nor"})
_AFTER_NEGATOR = re.compile(
    r"\s*(?:(?:is|are|was|were)\s+not\b|(?:is|are|was|were)n't\b|not\b|never\b"
    r"|prohibited\b|forbidden\b|unavailable\b|excluded\b)"
)
_NEGATION_WINDOW = 3
_LABEL_KEY = re.compile(r"[\s_-]+")
_TAG_BY_KEY = {tag.value: tag for tag in Amenity}


def _compile(fragments: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w)(?:{'|'.join(fragments)})(?!\w)")


# (tag, pattern, polarity): polarity True means the phrase affirms the tag.
_PATTERNS: tuple[tuple[Amenity, re.Pattern[str], bool], ...] = (
    *((tag, _compile(fragments), True) for tag, fragments in SYNONYMS.items()),
    *((tag, _compile(fragments), False) for tag, fragments in ABSENT_FORMS.items()),
)

Verdicts = dict[Amenity, bool]


def _negated(clause: str, start: int, end: int) -> bool:
    before = _TOKEN.findall(clause[:start])[-_NEGATION_WINDOW:]
    if any(word in _NEGATORS or word.endswith("n't") for word in before):
        return True
    return _AFTER_NEGATOR.match(clause, end) is not None


def _record(verdicts: Verdicts, tag: Amenity, present: bool) -> None:
    # Within one source a single "absent" outweighs any number of "present".
    verdicts[tag] = verdicts.get(tag, True) and present


def _scan(text: str) -> Verdicts:
    verdicts: Verdicts = {}
    normalized = text.lower().replace("\u2019", "'")
    for clause in _CLAUSE.split(normalized):
        for tag, pattern, polarity in _PATTERNS:
            for hit in pattern.finditer(clause):
                _record(verdicts, tag, polarity != _negated(clause, hit.start(), hit.end()))
    return verdicts


def _scan_labels(labels: Iterable[str]) -> tuple[Verdicts, frozenset[str]]:
    verdicts: Verdicts = {}
    unrecognized: set[str] = set()
    for label in labels:
        if tag := _TAG_BY_KEY.get(_LABEL_KEY.sub("_", label.strip().lower())):
            _record(verdicts, tag, True)
            continue
        found = _scan(label)
        if not found:
            unrecognized.add(label)
        for tag, present in found.items():
            _record(verdicts, tag, present)
    return verdicts, frozenset(unrecognized)


def reconcile_pets(verdicts: Verdicts) -> None:
    """ "Cats ok" implies pets are allowed; "no pets" rules out unstated cats and dogs."""
    if verdicts.get(Amenity.PETS_ALLOWED) is False:
        for kind in PET_KINDS:
            verdicts.setdefault(kind, False)
    if any(verdicts.get(kind) for kind in PET_KINDS):
        verdicts[Amenity.PETS_ALLOWED] = True


class KeywordAmenityExtractor:
    def extract(self, listing: NormalizedListing) -> AmenityResult:
        verdicts = _scan(f"{listing.title}\n{listing.description}")
        from_labels, unrecognized = _scan_labels(sorted(listing.amenities))
        verdicts.update(from_labels)
        reconcile_pets(verdicts)
        return AmenityResult(
            present=frozenset(tag for tag, present in verdicts.items() if present),
            absent=frozenset(tag for tag, present in verdicts.items() if not present),
            unrecognized=unrecognized,
        )
