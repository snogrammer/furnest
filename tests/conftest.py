"""Shared pytest configuration. Fixtures common to all test tiers go here."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from furnest.config import env_var_names, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep tests hermetic: no furnest env vars, no ``.env`` file, no cached settings.

    Settings read ``.env`` from the working directory, so each test runs in its own
    empty temp directory.
    """
    for name in env_var_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
