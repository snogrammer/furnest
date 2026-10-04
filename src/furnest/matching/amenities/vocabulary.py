"""Phrase tables for the keyword extractor: which wording means which :class:`Amenity`.

Each entry is a regex fragment matched against lower-cased text. The extractor wraps
every fragment in word boundaries, so ``ac`` doesn't fire inside "access" and ``deck``
doesn't fire inside "decked". Fragments are written for listing copy, not prose:
abbreviations (``w/d``, ``a/c``) and the usual qualifiers (``in-unit``, ``free``)
are spelled out.

:data:`SYNONYMS` are affirmative phrases ("dishwasher", "pets welcome"). A negator
in front ("no dishwasher") or after ("dishwasher not included") turns them into
"absent". :data:`ABSENT_FORMS` are phrases that already mean "absent" ("no pets",
"utilities extra") and have no affirmative noun to negate. The same negation rule
flips them back ("no smoking allowed" → smoke-free).

Lookbehinds keep look-alikes out: a shared or coin washer/dryer is not in-unit, a
pool table is not a pool, a leasing office is not a workspace.
"""

from __future__ import annotations

from furnest.domain.amenities import Amenity

_PERMIT = r"(?:are |is )?(?:allowed|ok|okay|welcome|considered|negotiable|accepted)"
_FORBID = r"(?:are |is )?not (?:allowed|permitted|accepted|ok|okay)"
_PAIR = r"\s*(?:/|and|&)\s*"
_NOT_SHARED = r"(?<!shared )(?<!coin )(?<!common )"


def _animal(word: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(affirmative, absent) phrases for one kind of animal."""
    affirmative = (rf"{word}s? {_PERMIT}", rf"{word}[- ]friendly")
    # "no pets", "no smoking or pets", "no dogs, cats or pets" (up to three list items).
    absent = (
        rf"no (?:[\w/-]+(?: or| and| &| nor) ){{0,3}}{word}s?",
        rf"{word}[- ]free",
        rf"{word}s? {_FORBID}",
    )
    return affirmative, absent


_PETS, _NO_PETS = _animal("pet")
_CATS, _NO_CATS = _animal("cat")
_DOGS, _NO_DOGS = _animal("dog")

SYNONYMS: dict[Amenity, tuple[str, ...]] = {
    Amenity.KING_BED: (r"(?:california |cal )?king(?:[- ]size[d]?)? (?:bed|mattress)s?",),
    Amenity.QUEEN_BED: (r"queen(?:[- ]size[d]?)? (?:bed|mattress)s?",),
    Amenity.DECK_PATIO: (
        r"decks?",
        r"patios?",
        r"porch(?:es)?",
        r"verandas?",
        r"roof ?top terrace",
    ),
    Amenity.BALCONY: (r"balcon(?:y|ies)",),
    Amenity.FREE_PARKING: (
        r"free (?:[\w-]+ ){0,2}parking",
        r"parking (?:is )?(?:free|included)",
        r"parking (?:space |spot )included",
        r"included parking",
    ),
    Amenity.GARAGE: (r"garages?(?! sale)",),
    Amenity.IN_UNIT_LAUNDRY: (
        rf"in[- ]unit (?:laundry|washer(?:{_PAIR}dryer)?|w/?d)",
        rf"{_NOT_SHARED}washer{_PAIR}dryer(?! on[- ]site)(?! in (?:the )?(?:building|basement))",
        rf"{_NOT_SHARED}w/d(?! on[- ]site)",
        r"private laundry",
        r"laundry in (?:the )?(?:unit|apartment|home|house)",
    ),
    Amenity.SHARED_LAUNDRY: (
        r"shared (?:laundry|washer|w/d)",
        r"(?:on[- ]site|coin(?:[- ]op(?:erated)?)?|common|communal) laundry",
        r"laundry (?:room |facilit(?:y|ies) )?(?:on[- ]site|in (?:the )?(?:building|basement))",
        rf"(?:washer{_PAIR}dryer|w/d) (?:on[- ]site|in (?:the )?(?:building|basement))",
    ),
    Amenity.PETS_ALLOWED: _PETS,
    Amenity.CATS_ALLOWED: _CATS,
    Amenity.DOGS_ALLOWED: _DOGS,
    Amenity.AC: (
        r"a/c",
        r"ac",
        r"air[- ]?condition(?:ing|ed|ers?)?",
        r"central air",
        r"mini[- ]splits?",
    ),
    Amenity.HEATING: (r"heat(?:ing)?", r"heaters?", r"heat pumps?", r"furnace"),
    Amenity.DISHWASHER: (r"dish ?washers?",),
    Amenity.WIFI: (r"wi-?fi", r"wireless internet", r"internet"),
    Amenity.UTILITIES_INCLUDED: (
        r"(?:all )?utilities (?:are )?(?:included|paid|incl)",
        r"all[- ]inclusive",
        r"bills (?:are )?included",
    ),
    Amenity.POOL: (r"pools?(?! table)",),
    Amenity.GYM: (r"gym", r"fitness (?:center|centre|room)", r"exercise room"),
    Amenity.ELEVATOR: (r"elevators?",),
    Amenity.WHEELCHAIR_ACCESSIBLE: (
        r"wheelchair[- ]access(?:ible)?",
        r"ada[- ](?:compliant|accessible)",
        r"handicap(?:ped)?[- ]accessible",
        r"step[- ]free",
    ),
    Amenity.FIREPLACE: (r"fire ?places?", r"wood[- ]?(?:burning )?stove"),
    Amenity.EV_CHARGING: (
        r"ev charg(?:er|ers|ing)",
        r"electric (?:vehicle|car) charg(?:er|ers|ing)",
        r"(?:tesla|level 2) chargers?",
    ),
    Amenity.WORKSPACE: (
        r"(?:dedicated )?work ?(?:space|station)s?",
        r"(?<!leasing )(?<!rental )(?<!post )(?<!management )(?<!sales )office",
        r"(?<!front )(?<!reception )(?<!help )desks?",
    ),
    Amenity.SMOKE_FREE: (
        r"no smoking",
        r"non[- ]?smoking",
        r"smoke[- ]free",
        r"smoking (?:is )?(?:not allowed|not permitted|prohibited|forbidden)",
    ),
}

ABSENT_FORMS: dict[Amenity, tuple[str, ...]] = {
    Amenity.PETS_ALLOWED: (*_NO_PETS, r"no animals"),
    Amenity.CATS_ALLOWED: _NO_CATS,
    Amenity.DOGS_ALLOWED: _NO_DOGS,
    Amenity.FREE_PARKING: (
        r"parking (?:is )?(?:not included|extra|additional)",
        r"paid parking",
        r"no parking",
    ),
    Amenity.UTILITIES_INCLUDED: (
        r"utilities (?:are )?(?:not included|extra|separate|additional)",
        r"plus utilities",
        r"tenants? pays? (?:all )?utilities",
    ),
    Amenity.SMOKE_FREE: (r"smoking (?:is )?(?:allowed|ok|okay|permitted)", r"smoker[- ]friendly"),
}

# Stating the specific tag implies the general one; ruling out the general rules out
# whichever specific ones are still unknown.
PET_KINDS: tuple[Amenity, ...] = (Amenity.CATS_ALLOWED, Amenity.DOGS_ALLOWED)
