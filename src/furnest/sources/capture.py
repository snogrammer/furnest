"""Recording a live page as a sanitized adapter fixture (``scripts/capture_fixture.py``).

This is a developer tool. It is run by hand when an adapter's fixtures need to be
refreshed, never in CI or by the worker, so it refuses to run when ``CI`` is set.

The page is fetched the way the adapter itself would fetch it: through a
:class:`~furnest.sources.http.SourceHttpClient` built from the adapter's resolved
config. That means the same user agent, proxy, rate limit, robots.txt rules and
block detection. A blocked page raises :class:`SourceBlockedError` and nothing is
written. There is deliberately no way around a block (see ``docs/adapters.md``).

Sources backed by Apify (:mod:`furnest.sources.apify`) are captured from the API.
For example, a dataset of a run made in the Apify console:
``https://api.apify.com/v2/datasets/<id>/items?clean=true&format=json``. For that host
the adapter's ``api_key`` goes in the ``Authorization`` header, and a token in the URL
is refused so that it can't end up in a log or in shell history.

The body is run through :func:`furnest.sources.sanitize.sanitize` and written to
``<out_dir>/<adapter>/<name>``. An existing file is replaced only with ``force``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

import httpx2

from furnest.config import SourcesSettings
from furnest.sources.http import SourceHttpClient
from furnest.sources.registry import SourceRegistry, discover
from furnest.sources.sanitize import sanitize

APIFY_HOST = "api.apify.com"
_MAX_NAME_LENGTH = 60


class CaptureError(Exception):
    """The capture can't run as asked; nothing was fetched or written."""


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    content_type: str
    # Replacements the sanitizer made, by category.
    counts: Mapping[str, int]


def capture_fixture(
    adapter: str,
    url: str,
    *,
    settings: SourcesSettings,
    out_dir: Path,
    name: str | None = None,
    redact: Iterable[str] = (),
    force: bool = False,
    registry: SourceRegistry | None = None,
    transport: httpx2.BaseTransport | None = None,
    environ: Mapping[str, str] = os.environ,
) -> CaptureResult:
    """Fetch ``url`` as ``adapter`` would, sanitize it, and write it as a fixture."""
    if environ.get("CI"):
        msg = "fixture capture is for manual use only and does not run in CI"
        raise CaptureError(msg)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        msg = f"not an http(s) URL: {url}"
        raise CaptureError(msg)
    if name is not None and (not name or PurePosixPath(name).name != name or name in {".", ".."}):
        msg = f"--name must be a plain file name, got {name!r}"
        raise CaptureError(msg)

    registry = registry or discover()
    sources = {source.config.name: source for source in registry.configured(settings)}
    if adapter not in sources:
        msg = f"unknown adapter {adapter!r}; known: {', '.join(sorted(sources)) or 'none'}"
        raise CaptureError(msg)
    config = sources[adapter].config

    headers: dict[str, str] = {}
    if parts.hostname == APIFY_HOST:
        if "token" in parse_qs(parts.query):
            msg = f'remove "token" from the URL; set "api_key" for {adapter} in SOURCES_ADAPTERS'
            raise CaptureError(msg)
        if config.api_key is None:
            msg = (
                f'set "api_key" (an Apify API token) for {adapter} in SOURCES_ADAPTERS; '
                "this script reads the token from the environment only"
            )
            raise CaptureError(msg)
        headers["Authorization"] = f"Bearer {config.api_key.get_secret_value()}"
        # An authenticated API call, not crawling: see furnest.sources.apify.
        config = replace(config, respect_robots=False)

    folder = out_dir / adapter
    if name is not None:
        _check_free(folder / name, force=force)

    with SourceHttpClient(config, transport=transport) as http:
        response = http.get(url, headers=headers)

    content_type = response.headers.get("content-type", "")
    path = folder / (name or default_name(url, content_type))
    _check_free(path, force=force)
    result = sanitize(response.text, content_type, redact=redact)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(result.text, encoding="utf-8")
    return CaptureResult(path=path, content_type=content_type, counts=result.counts)


def default_name(url: str, content_type: str) -> str:
    """A file name from the URL path, e.g. ``datasets-abc-items.json``."""
    path = PurePosixPath(unquote(urlsplit(url).path))
    stem = "-".join([*path.parent.parts[1:], path.stem]) if path.parts else ""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[-_MAX_NAME_LENGTH:].strip("-")
    return f"{slug or 'index'}{_suffix(content_type)}"


def _suffix(content_type: str) -> str:
    content_type = content_type.lower()
    if "json" in content_type:
        return ".json"
    if "html" in content_type:
        return ".html"
    return ".txt"


def _check_free(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        msg = f"{path} exists; pass --force to replace it"
        raise CaptureError(msg)
