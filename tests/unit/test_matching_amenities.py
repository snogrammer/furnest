"""KeywordAmenityExtractor: synonyms, negations, clause scoping and source precedence."""

import pytest

from furnest.domain import Amenity, AmenityResult, NormalizedListing
from furnest.matching.amenities import KeywordAmenityExtractor

A = Amenity
extractor = KeywordAmenityExtractor()


def extract(
    description: str = "", *, title: str = "Furnished 1BR", **fields: object
) -> AmenityResult:
    listing = NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "1",
            "url": "https://example.com/1",
            "title": title,
            "description": description,
            **fields,
        }
    )
    return extractor.extract(listing)


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("King-size bed", A.KING_BED),
        ("california king mattress", A.KING_BED),
        ("Queen bed in the second room", A.QUEEN_BED),
        ("Private deck", A.DECK_PATIO),
        ("Sunny patio", A.DECK_PATIO),
        ("Front porch", A.DECK_PATIO),
        ("Juliet balcony", A.BALCONY),
        ("Free street parking", A.FREE_PARKING),
        ("Off-street parking free", A.FREE_PARKING),
        ("Parking included", A.FREE_PARKING),
        ("Attached garage", A.GARAGE),
        ("W/D in unit", A.IN_UNIT_LAUNDRY),
        ("Washer & dryer in unit", A.IN_UNIT_LAUNDRY),
        ("In-unit washer/dryer", A.IN_UNIT_LAUNDRY),
        ("Laundry in the apartment", A.IN_UNIT_LAUNDRY),
        ("Shared laundry", A.SHARED_LAUNDRY),
        ("Coin-op laundry", A.SHARED_LAUNDRY),
        ("Laundry room on-site", A.SHARED_LAUNDRY),
        ("Washer/dryer in the basement", A.SHARED_LAUNDRY),
        ("Pets welcome", A.PETS_ALLOWED),
        ("Pet-friendly building", A.PETS_ALLOWED),
        ("Cats OK", A.CATS_ALLOWED),
        ("Small dogs are allowed", A.DOGS_ALLOWED),
        ("Central A/C", A.AC),
        ("AC in every room", A.AC),
        ("Air-conditioned", A.AC),
        ("Ductless mini-split", A.AC),
        ("Radiant heat", A.HEATING),
        ("Dishwasher", A.DISHWASHER),
        ("Fast Wi-Fi", A.WIFI),
        ("Fiber internet", A.WIFI),
        ("All utilities included", A.UTILITIES_INCLUDED),
        ("All-inclusive rent", A.UTILITIES_INCLUDED),
        ("Heated pool", A.POOL),
        ("Fitness center", A.GYM),
        ("Elevator building", A.ELEVATOR),
        ("ADA-compliant unit", A.WHEELCHAIR_ACCESSIBLE),
        ("Wood-burning stove", A.FIREPLACE),
        ("EV charger in the garage", A.EV_CHARGING),
        ("Dedicated workspace", A.WORKSPACE),
        ("Home office", A.WORKSPACE),
        ("Standing desk", A.WORKSPACE),
        ("Non-smoking unit", A.SMOKE_FREE),
        ("Smoke-free building", A.SMOKE_FREE),
        ("No smoking", A.SMOKE_FREE),
        ("Smoking is prohibited", A.SMOKE_FREE),
        ("No smoking allowed", A.SMOKE_FREE),
    ],
)
def test_synonyms(text: str, tag: Amenity) -> None:
    result = extract(text)
    assert tag in result.present
    assert not result.absent & {tag}


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("No pets", A.PETS_ALLOWED),
        ("Pets not allowed", A.PETS_ALLOWED),
        ("Pets are not allowed", A.PETS_ALLOWED),
        ("Pet-free home", A.PETS_ALLOWED),
        ("Not pet friendly", A.PETS_ALLOWED),
        ("No animals", A.PETS_ALLOWED),
        ("No smoking or pets", A.PETS_ALLOWED),
        ("Sorry, no dogs", A.DOGS_ALLOWED),
        ("No cats or dogs", A.CATS_ALLOWED),
        ("No cats or dogs", A.DOGS_ALLOWED),
        ("No A/C", A.AC),
        ("No dishwasher or AC", A.AC),
        ("No dishwasher or AC", A.DISHWASHER),
        ("Without a dishwasher", A.DISHWASHER),
        ("Dishwasher not included", A.DISHWASHER),
        ("The dishwasher isn't working", A.DISHWASHER),
        ("There's no elevator", A.ELEVATOR),
        ("Doesn't have a balcony", A.BALCONY),
        ("Parking not included", A.FREE_PARKING),
        ("Paid parking only", A.FREE_PARKING),
        ("Utilities not included", A.UTILITIES_INCLUDED),
        ("Rent plus utilities", A.UTILITIES_INCLUDED),
        ("Tenant pays utilities", A.UTILITIES_INCLUDED),
        ("Smoking allowed on the patio", A.SMOKE_FREE),
    ],
)
def test_negations(text: str, tag: Amenity) -> None:
    result = extract(text)
    assert tag in result.absent
    assert tag not in result.present


@pytest.mark.parametrize(
    ("text", "present", "absent"),
    [
        # Negation stops at a clause boundary.
        ("No smoking, pets welcome", {A.SMOKE_FREE, A.PETS_ALLOWED}, set()),
        ("No smoking but pets welcome", {A.SMOKE_FREE, A.PETS_ALLOWED}, set()),
        ("No dishwasher. Central A/C", {A.AC}, {A.DISHWASHER}),
        ("Dishwasher, no A/C", {A.DISHWASHER}, {A.AC}),
        # A negator more than three words back is out of range.
        ("Not far from the pool", {A.POOL}, set()),
        # Within the text, absent wins over present.
        ("Pets considered. No pets over 50 lbs", set(), {A.PETS_ALLOWED}),
    ],
)
def test_clause_scoping(text: str, present: set[Amenity], absent: set[Amenity]) -> None:
    result = extract(text)
    assert result.present == present
    assert result.absent >= absent


@pytest.mark.parametrize(
    "text",
    [
        "Easy access to downtown",  # "ac" inside a word
        "Pool table in the den",
        "Leasing office open daily",
        "Ask at the front desk",
        "Shared W/D",  # shared, not in-unit
        "Garage sale every weekend",
    ],
)
def test_look_alikes_do_not_match(text: str) -> None:
    result = extract(text)
    assert not result.present & {
        A.AC,
        A.POOL,
        A.WORKSPACE,
        A.IN_UNIT_LAUNDRY,
        A.GARAGE,
    }


def test_title_is_scanned_too() -> None:
    assert extract(title="Cozy studio with balcony").status("balcony") is True


def test_empty_listing_has_no_tags() -> None:
    assert extract() == AmenityResult()


def test_curly_apostrophe_negates() -> None:
    assert extract("The dishwasher isn\u2019t working").status("dishwasher") is False


@pytest.mark.parametrize(
    ("labels", "present", "absent"),
    [
        (["in_unit_laundry"], {A.IN_UNIT_LAUNDRY}, set()),
        (["In-Unit Laundry"], {A.IN_UNIT_LAUNDRY}, set()),
        (["  Free Parking  "], {A.FREE_PARKING}, set()),
        (["Air Conditioning", "Washer/Dryer"], {A.AC, A.IN_UNIT_LAUNDRY}, set()),
        (["No Pets"], set(), {A.PETS_ALLOWED, A.CATS_ALLOWED, A.DOGS_ALLOWED}),
        (["Pets allowed", "No pets"], set(), {A.PETS_ALLOWED}),
    ],
)
def test_adapter_labels(labels: list[str], present: set[Amenity], absent: set[Amenity]) -> None:
    result = extract(amenities=labels)
    assert result.present == present
    assert result.absent >= absent
    assert result.unrecognized == frozenset()


def test_unrecognized_labels_are_reported() -> None:
    result = extract(amenities=["Kitchen", "Dishwasher", "Parking"])
    assert result.present == {A.DISHWASHER}
    assert result.unrecognized == {"kitchen", "parking"}


def test_adapter_label_overrides_text() -> None:
    result = extract("No pets", amenities=["Pets allowed"])
    assert result.status("pets_allowed") is True


def test_adapter_absence_overrides_text() -> None:
    result = extract("Dishwasher", amenities=["No dishwasher"])
    assert result.status("dishwasher") is False


@pytest.mark.parametrize(
    ("text", "present", "absent"),
    [
        ("No dogs, cats OK", {A.CATS_ALLOWED, A.PETS_ALLOWED}, {A.DOGS_ALLOWED}),
        ("Dogs welcome", {A.DOGS_ALLOWED, A.PETS_ALLOWED}, set()),
        ("No pets", set(), {A.PETS_ALLOWED, A.CATS_ALLOWED, A.DOGS_ALLOWED}),
        # A specific "yes" outranks a general "no".
        ("No pets, cats OK", {A.CATS_ALLOWED, A.PETS_ALLOWED}, {A.DOGS_ALLOWED}),
    ],
)
def test_pet_implications(text: str, present: set[Amenity], absent: set[Amenity]) -> None:
    result = extract(text)
    assert result.present == present
    assert result.absent == absent
