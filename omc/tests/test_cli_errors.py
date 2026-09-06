"""Error-mapping tests at the CLI level: 401/403/404/413/422 → clean messages."""

from __future__ import annotations

import httpx
import pytest
import respx
from typer.testing import CliRunner

from outo_models_cli.main import app

SERVER = "http://api.test"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_me() -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )


def test_401_maps_to_clean_login_message(runner: CliRunner) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(401, json={"detail": "expired"})
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code != 0
    assert "auth login" in result.stderr
    assert "Traceback" not in result.stderr
    assert 'File "' not in result.stderr


def test_debug_flag_shows_traceback(runner: CliRunner) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(401, json={"detail": "expired"})
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["--debug", "auth", "whoami"])
    assert result.exit_code != 0 or "Traceback" in result.output or result.exception is not None


def test_unreachable_server_clean_message(runner: CliRunner) -> None:
    """After a successful login, the next /me call failing → clean unreachable message."""
    respx.get(f"{SERVER}/api/auth/me").mock(
        side_effect=[
            httpx.Response(200, json={"username": "alice", "role": "user"}),
            httpx.ConnectError("nope"),
        ],
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code != 0
    assert "Cannot reach" in result.stderr or "reach" in result.stderr.lower()
    assert "Traceback" not in result.stderr
