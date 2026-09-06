"""End-to-end CLI tests for the `omc auth ...` sub-app."""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from outo_models_cli import config
from outo_models_cli.main import app

SERVER = "http://api.test"
OTHER = "http://other.test"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_me() -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )


# ---------------------------------------------------------------------------
# auth login
# ---------------------------------------------------------------------------


def test_auth_login_verifies_then_stores(runner: CliRunner, config_path: Path) -> None:
    """`auth login` calls /api/auth/me, persists the token, reports the username."""
    _mock_me()
    result = runner.invoke(
        app,
        ["auth", "login", "--server", SERVER, "--token", "PAT-SECRET"],
    )
    assert result.exit_code == 0, result.output
    assert config_path.exists()
    store = config.Store.load(config_path)
    assert store.servers[SERVER].token == "PAT-SECRET"
    assert store.default_server == SERVER
    assert "alice" in result.stdout
    assert "PAT-SECRET" not in result.stdout
    assert "PAT-SECRET" not in result.stderr


def test_auth_login_rejects_bad_token(runner: CliRunner, config_path: Path) -> None:
    """401 from /api/auth/me → no credential is persisted."""
    respx.get(f"{SERVER}/api/auth/me").respond(401, json={"detail": "expired"})
    result = runner.invoke(
        app,
        ["auth", "login", "--server", SERVER, "--token", "BAD"],
    )
    assert result.exit_code != 0
    assert "rejected" in result.stderr.lower() or "not authenticated" in result.stderr.lower()
    assert (not config_path.exists()) or config_path.read_text() in ("", None)


def test_auth_login_normalizes_server_url(
    runner: CliRunner,
    config_path: Path,
) -> None:
    """`192.168.0.10` (no scheme) is stored under `http://192.168.0.10`."""
    respx.get("http://192.168.0.10/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    result = runner.invoke(
        app,
        ["auth", "login", "--server", "192.168.0.10", "--token", "t"],
    )
    assert result.exit_code == 0, result.output
    store = config.Store.load(config_path)
    assert "http://192.168.0.10" in store.servers


def test_auth_login_creates_file_with_0600_mode(
    runner: CliRunner,
    config_path: Path,
) -> None:
    """`stat -c %a` on the persisted file → 600."""
    _mock_me()
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    assert config_path.exists()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_auth_login_no_token_arg_aborts(runner: CliRunner) -> None:
    """Interactive prompt can't run inside CliRunner — `--token` must be set."""
    _mock_me()
    result = runner.invoke(app, ["auth", "login", "--server", SERVER])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Multi-server + set-default
# ---------------------------------------------------------------------------


def test_multi_server_config(runner: CliRunner, config_path: Path) -> None:
    """Two logins produce a config with both servers, first becomes default."""
    for srv in (SERVER, OTHER):
        respx.get(f"{srv}/api/auth/me").respond(
            200,
            json={"username": "alice", "role": "user"},
        )
        runner.invoke(app, ["auth", "login", "--server", srv, "--token", f"pat-{srv}"])
    store = config.Store.load(config_path)
    assert set(store.servers) == {SERVER, OTHER}
    assert store.default_server == SERVER


def test_auth_login_set_default_swaps_target(
    runner: CliRunner,
    config_path: Path,
) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    respx.get(f"{OTHER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    runner.invoke(
        app,
        ["auth", "login", "--server", OTHER, "--token", "t", "--set-default"],
    )
    store = config.Store.load(config_path)
    assert store.default_server == OTHER


# ---------------------------------------------------------------------------
# auth logout
# ---------------------------------------------------------------------------


def test_auth_logout_removes_one_entry(runner: CliRunner, config_path: Path) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    respx.get(f"{OTHER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    runner.invoke(app, ["auth", "login", "--server", OTHER, "--token", "t"])
    runner.invoke(app, ["auth", "logout", "--server", SERVER])
    store = config.Store.load(config_path)
    assert SERVER not in store.servers
    assert OTHER in store.servers


def test_auth_logout_aborts_when_no_entry(runner: CliRunner) -> None:
    result = runner.invoke(app, ["auth", "logout", "--server", "http://nope"])
    assert result.exit_code != 0
    assert "No stored credential" in result.stderr


# ---------------------------------------------------------------------------
# auth whoami
# ---------------------------------------------------------------------------


def test_auth_whoami_uses_stored_token(runner: CliRunner) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "admin"},
    )
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "admin"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code == 0, result.stderr
    assert "alice" in result.stdout
    assert "admin" in result.stdout


def test_auth_whoami_prefers_env_token(runner: CliRunner) -> None:
    """`OMC_TOKEN` should never be echoed — only its SOURCE label."""
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    import os

    os.environ["OMC_SERVER"] = SERVER
    os.environ["OMC_TOKEN"] = "override-token-shh"
    try:
        result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0, result.stderr
        assert "OMC_TOKEN" in result.stdout  # label, not the value
        assert "override-token-shh" not in result.stdout
        assert "override-token-shh" not in result.stderr
    finally:
        os.environ.pop("OMC_TOKEN", None)
        os.environ.pop("OMC_SERVER", None)


# ---------------------------------------------------------------------------
# auth status
# ---------------------------------------------------------------------------


def test_auth_status_lists_configured_servers(runner: CliRunner) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    respx.get(f"{OTHER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    runner.invoke(app, ["auth", "login", "--server", OTHER, "--token", "t"])
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert SERVER in result.stdout
    assert OTHER in result.stdout


def test_auth_status_empty(runner: CliRunner) -> None:
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "No servers configured" in result.stdout


_ = httpx
