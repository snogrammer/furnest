"""Amenity vocabulary, AmenityResult invariants and the extractor protocol."""

import dataclasses
import re

import pytest

from furnest.domain import Amenity, AmenityExtractor, AmenityResult, NormalizedListing


def test_tags_are_unique_snake_case() -> None:
    values = [tag.value for tag in Amenity]
    assert len(values) == len(set(values))
    assert all(re.fullmatch(r"[a-z]+(?:_[a-z]+)*", value) for value in values)


def test_tags_compare_equal_to_criteria_strings() -> None:
    result = AmenityResult(present=frozenset({Amenity.POOL}))
    assert "pool" in result.present


def test_status_is_present_absent_or_unknown() -> None:
    result = AmenityResult(present=frozenset({Amenity.AC}), absent=frozenset({Amenity.GYM}))
    assert result.status("ac") is True
    assert result.status(Amenity.GYM) is False
    assert result.status("pool") is None
    assert result.status("not_a_tag") is None


def test_result_rejects_a_tag_both_present_and_absent() -> None:
    with pytest.raises(ValueError, match="both present and absent"):
        AmenityResult(present=frozenset({Amenity.AC}), absent=frozenset({Amenity.AC}))


def test_result_is_frozen() -> None:
    result = AmenityResult()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.present = frozenset({Amenity.AC})  # type: ignore[misc]


def test_any_object_with_extract_is_an_extractor() -> None:
    class Fixed:
        def extract(self, listing: NormalizedListing) -> AmenityResult:
            return AmenityResult(present=frozenset({Amenity.WIFI}))

    extractor: AmenityExtractor = Fixed()
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "1", "url": "https://example.com/1", "title": "1BR"}
    )
    assert extractor.extract(listing).status("wifi") is True
