"""Listing photos: bounded download, perceptual hash, Hamming distance.

The same flat can show up on several sources with the same photos, re-encoded,
resized or recropped. A perceptual hash (pHash, from ``imagehash``) changes little
under those edits. Two photos are probably the same picture when their hashes
differ in at most :data:`DEFAULT_MATCH_DISTANCE` of 64 bits.

:class:`ImageFetcher` downloads one photo into memory, with limits so a hostile or
broken URL can't cost much:

* robots.txt of the image host is obeyed when ``SOURCES_RESPECT_ROBOTS`` is set,
  as for listing pages;
* each host gets its own token bucket (``IMAGES_REQUESTS_PER_MINUTE``);
* the response must be ``image/*``, and is abandoned as soon as its declared or
  streamed size passes ``IMAGES_MAX_BYTES``;
* the host (and every redirect hop) must resolve to public addresses only; see
  :mod:`furnest.netguard`;
* there are no retries. The caller records the failure and moves on.

:func:`compute_phash` refuses images with more than ``IMAGES_MAX_PIXELS`` pixels
before decoding them. Nothing here writes a photo anywhere; only the hash is kept.
Hashes are 16 lower-case hex digits, the form ``str(imagehash.ImageHash)`` gives.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import httpx2
import imagehash
from PIL import Image, UnidentifiedImageError

from furnest.config import ImageSettings, SourcesSettings
from furnest.netguard import Resolver, UnsafeAddressError, guarded_client_args
from furnest.sources.errors import SourceError
from furnest.sources.ratelimit import TokenBucket
from furnest.sources.robots import RobotsCache

# Hashes at most this many bits apart are treated as the same photo.
DEFAULT_MATCH_DISTANCE = 8
# The ``IMAGES_MAX_PIXELS`` default: 40 megapixels.
DEFAULT_MAX_PIXELS = 40_000_000
# ``IMAGES_TIMEOUT_SECONDS`` bounds each read; the whole download gets this many times
# that, so a host dripping bytes just inside the read timeout can't hold a fetch forever.
_TOTAL_TIMEOUT_FACTOR = 4


class ImageFetchError(Exception):
    """A photo couldn't be fetched or hashed. ``reason`` is short enough to store."""

    def __init__(self, reason: str, *, url: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.url = url


# --- Hashing ------------------------------------------------------------------------


def compute_phash(data: bytes, *, max_pixels: int = DEFAULT_MAX_PIXELS) -> str:
    """The pHash of an encoded image, as 16 hex digits."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > max_pixels:
                msg = f"too many pixels ({width}x{height})"
                raise ImageFetchError(msg)
            # JPEGs can decode straight to a small greyscale image; pHash needs 32x32.
            image.draft("L", (128, 128))
            return str(imagehash.phash(image))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ImageFetchError("not an image") from exc


def hamming(a: str, b: str) -> int:
    """How many bits two hashes differ in. Raises ``ValueError`` on a malformed hash."""
    if len(a) != len(b):
        msg = f"hashes differ in length: {a!r}, {b!r}"
        raise ValueError(msg)
    return (int(a, 16) ^ int(b, 16)).bit_count()


def min_distance(left: Iterable[str], right: Iterable[str]) -> int | None:
    """The smallest distance between any photo of ``left`` and any of ``right``,
    or ``None`` when either side has no hashes."""
    right = list(right)
    distances = [hamming(a, b) for a in left for b in right]
    return min(distances, default=None)


def matching_pairs(
    left: Iterable[str], right: Iterable[str], *, max_distance: int = DEFAULT_MATCH_DISTANCE
) -> int:
    """How many photos of ``left`` have a near-identical photo in ``right``."""
    right = list(right)
    return sum(1 for a in left if any(hamming(a, b) <= max_distance for b in right))


# --- Fetching -----------------------------------------------------------------------


class ImageFetcher:
    """Downloads photos into memory. Tests pass an ``httpx2.MockTransport`` and a fake
    clock and sleep."""

    def __init__(
        self,
        settings: ImageSettings,
        sources: SourcesSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._user_agent = sources.user_agent
        self._respect_robots = sources.respect_robots
        self._clock = clock
        self._sleep = sleep
        proxy = str(sources.proxy_url) if sources.proxy_url else None
        self._http = httpx2.Client(
            timeout=httpx2.Timeout(float(settings.timeout_seconds)),
            headers={"User-Agent": sources.user_agent, "Accept": "image/*"},
            proxy=proxy,
            follow_redirects=True,
            **guarded_client_args(
                transport=transport,
                proxy=proxy,
                allow_private=sources.allow_private_addresses,
                resolver=resolver,
            ),
        )
        self._limiters: dict[str, TokenBucket] = {}
        self.robots = RobotsCache(self._fetch_robots, clock=clock)

    def fetch(self, url: str) -> bytes:
        """The photo's bytes. Raises :class:`ImageFetchError` with the reason."""
        try:
            if self._respect_robots and not self.robots.allowed(url, self._user_agent):
                raise ImageFetchError("robots.txt", url=url)
        except SourceError as exc:
            raise ImageFetchError("robots.txt unreachable", url=url) from exc
        self._limiter(url).acquire()
        deadline = self._clock() + self._settings.timeout_seconds * _TOTAL_TIMEOUT_FACTOR
        try:
            with self._http.stream("GET", url) as response:
                if response.is_error:
                    raise ImageFetchError(f"HTTP {response.status_code}", url=url)
                return self._read(response, url, deadline)
        except httpx2.TimeoutException as exc:
            raise ImageFetchError("timeout", url=url) from exc
        except UnsafeAddressError as exc:
            raise ImageFetchError("blocked address", url=url) from exc
        except httpx2.HTTPError as exc:
            raise ImageFetchError(f"transport error: {type(exc).__name__}", url=url) from exc

    def fetch_phash(self, url: str) -> str:
        return compute_phash(self.fetch(url), max_pixels=self._settings.max_pixels)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Internals ----------------------------------------------------------------

    def _read(self, response: httpx2.Response, url: str, deadline: float) -> bytes:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise ImageFetchError(f"not an image ({content_type or 'no content-type'})", url=url)
        limit = self._settings.max_bytes
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise ImageFetchError("too large", url=url)
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ImageFetchError("too large", url=url)
            if self._clock() > deadline:
                raise ImageFetchError("timeout", url=url)
        return bytes(body)

    def _limiter(self, url: str) -> TokenBucket:
        host = urlsplit(url).netloc.lower()
        limiter = self._limiters.get(host)
        if limiter is None:
            limiter = TokenBucket(
                self._settings.requests_per_minute, clock=self._clock, sleep=self._sleep
            )
            self._limiters[host] = limiter
        return limiter

    def _fetch_robots(self, robots_url: str) -> tuple[int, str]:
        self._limiter(robots_url).acquire()
        try:
            response = self._http.get(robots_url)
        except (httpx2.HTTPError, UnsafeAddressError) as exc:
            msg = f"robots.txt fetch failed: {type(exc).__name__}"
            raise SourceError(msg, url=robots_url) from exc
        return response.status_code, response.text
