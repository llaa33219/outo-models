"""Shared pytest fixtures for the `outo-models-cli` package.

Each test gets an isolated config directory under `tmp_path` so the
real `~/.config/omc/config.json` cannot leak between cases. The
`cli_runner` fixture wraps Typer's `CliRunner` so a single `runner.invoke(...)`
call drives the full CLI surface without standing up subprocesses.

The fixtures are deliberately small — every test owns its own
`respx` mock so a wire-format change surfaces in exactly one place.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolate_omc_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point every `OMC_*` env var at the test's temp directory.

    `config.config_path()` consults `OMC_CONFIG_DIR` first, so a single
    env-var override is enough to keep `auth login` from touching the
    host's `~/.config/omc/config.json`. The fixture also clears any
    `OMC_SERVER` / `OMC_TOKEN` the host shell may have set so tests
    that assert "no token source" actually have no token source.
    """
    monkeypatch.setenv("OMC_CONFIG_DIR", str(tmp_path / "omc-cfg"))
    monkeypatch.delenv("OMC_SERVER", raising=False)
    monkeypatch.delenv("OMC_TOKEN", raising=False)
    yield


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Return the per-test config directory (also set via `OMC_CONFIG_DIR`)."""
    path = tmp_path / "omc-cfg"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def config_path(config_dir: Path) -> Path:
    """Return the per-test config JSON path."""
    return config_dir / "config.json"


@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a Typer `CliRunner` bound to the root `app`."""
    return CliRunner(mix_stderr=False)


@pytest.fixture
def server_url() -> str:
    """Default server URL used by the API tests."""
    return "http://testserver"


@pytest.fixture
def token() -> str:
    """Default bearer token used by the API tests."""
    return "test-token-shhh"


@pytest.fixture(autouse=True)
def _clean_respx(respx_mock) -> Iterator[None]:
    """Each test starts with an empty `respx` mock — explicit is better.

    `respx_mock` is provided by `pytest-respx`'s plugin (we import
    `respx` which auto-registers it). Re-asserting it here documents
    the fixture's purpose and keeps the autouse load order obvious.
    """
    yield


# The fixture above imports `respx_mock` lazily so the plugin's import
# side effects run exactly once per test session.
_ = os
