"""pHash on local fixture photos, and the Hamming-distance helpers."""

import io
from pathlib import Path

import pytest
from PIL import Image

from furnest.dedup.phash import (
    DEFAULT_MATCH_DISTANCE,
    ImageFetchError,
    compute_phash,
    hamming,
    matching_pairs,
    min_distance,
)

IMAGES = Path(__file__).parents[1] / "fixtures" / "images"


def phash_of(name: str) -> str:
    return compute_phash((IMAGES / name).read_bytes())


def test_phash_is_sixteen_hex_digits_and_stable() -> None:
    first = phash_of("room.jpg")

    assert len(first) == 16
    int(first, 16)
    assert phash_of("room.jpg") == first


@pytest.mark.parametrize("variant", ["room_resized.jpg", "room.png"])
def test_a_resized_or_reencoded_copy_is_within_match_distance(variant: str) -> None:
    assert hamming(phash_of("room.jpg"), phash_of(variant)) <= DEFAULT_MATCH_DISTANCE


def test_a_different_photo_is_far_away() -> None:
    assert hamming(phash_of("room.jpg"), phash_of("other.jpg")) > 3 * DEFAULT_MATCH_DISTANCE


def test_non_image_bytes_are_rejected() -> None:
    with pytest.raises(ImageFetchError, match="not an image"):
        compute_phash((IMAGES / "not_image.txt").read_bytes())


def test_truncated_image_is_rejected() -> None:
    data = (IMAGES / "room.jpg").read_bytes()

    with pytest.raises(ImageFetchError, match="not an image"):
        compute_phash(data[: len(data) // 3])


def test_too_many_pixels_are_rejected_before_decoding() -> None:
    buffer = io.BytesIO()
    Image.new("L", (200, 200)).save(buffer, format="PNG")

    with pytest.raises(ImageFetchError, match="too many pixels"):
        compute_phash(buffer.getvalue(), max_pixels=200 * 200 - 1)


def test_hamming_counts_differing_bits() -> None:
    assert hamming("0000000000000000", "0000000000000000") == 0
    assert hamming("0000000000000000", "000000000000000f") == 4
    assert hamming("ffffffffffffffff", "0000000000000000") == 64


def test_hamming_rejects_malformed_hashes() -> None:
    with pytest.raises(ValueError, match="length"):
        hamming("00", "0000")
    with pytest.raises(ValueError, match="invalid literal"):
        hamming("zz", "00")


def test_min_distance() -> None:
    a, b, c = "0000000000000000", "0000000000000001", "00000000000000ff"

    assert min_distance([a], [c, b]) == 1
    assert min_distance([], [a]) is None
    assert min_distance([a], []) is None


def test_matching_pairs_counts_left_photos_with_a_near_match() -> None:
    a, near_a, far = "0000000000000000", "0000000000000003", "ffffffffffffffff"

    assert matching_pairs([a, far], [near_a]) == 1
    assert matching_pairs([a, a], [near_a]) == 2
    assert matching_pairs([a], [near_a], max_distance=1) == 0
    assert matching_pairs([], [a]) == 0
