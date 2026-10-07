import pytest

from furnest.dedup.text import MAX_TEXT_LENGTH, normalize_text, text_similarity

pytestmark = pytest.mark.unit


def test_normalize_text_lowercases_and_drops_punctuation() -> None:
    assert normalize_text("  Furnished 2BR -- near DOWNTOWN!! ") == "furnished 2br near downtown"


def test_normalize_text_cuts_long_text() -> None:
    assert len(normalize_text("a" * (MAX_TEXT_LENGTH + 500))) == MAX_TEXT_LENGTH


@pytest.mark.parametrize(("a", "b"), [(None, "x"), ("", "x"), ("x", "  !! ")])
def test_empty_text_has_no_similarity(a: str | None, b: str | None) -> None:
    assert text_similarity(a, b) is None


def test_reordered_words_are_similar() -> None:
    similarity = text_similarity(
        "Furnished 2BR near downtown, free parking", "free parking - furnished 2BR near downtown"
    )
    assert similarity == 1.0


def test_different_ads_are_not_similar() -> None:
    similarity = text_similarity("Furnished 2BR near downtown", "Studio by the lake, pets ok")
    assert similarity is not None
    assert similarity < 0.5
