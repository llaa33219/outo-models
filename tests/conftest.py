"""Shared pytest fixtures for all test suites.

Provides a per-test `data_dir` rooted under pytest's `tmp_path`, so every test
gets an isolated, writable filesystem with no risk of polluting `/var/lib/outo-models`,
and clears `OUTO_*` environment variables so the defaults tests stay
deterministic regardless of the host's shell.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from outo_models.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_outo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every `OUTO_*` environment variable before each test.

    `Settings(_env_file=None)` still reads environment variables; without
    this fixture, defaults tests would silently leak from the host shell.
    Individual tests opt back in via the `tmp_data_dir` fixture, which sets
    `OUTO_DATA_DIR` explicitly.
    """
    for name in [k for k in os.environ if k.startswith("OUTO_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp data directory and wire it into the Settings via OUTO_DATA_DIR.

    Clears the `get_settings()` `lru_cache` before and after the test so that
    each test sees a fresh Settings bound to its own `tmp_data_dir`.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OUTO_DATA_DIR", str(data))
    get_settings.cache_clear()
    try:
        yield data
    finally:
        get_settings.cache_clear()


@pytest.fixture
def settings(tmp_data_dir: Path) -> Settings:
    """Return a fresh `Settings` instance bound to the per-test data dir."""
    return get_settings()
