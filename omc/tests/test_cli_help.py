"""Smoke tests: --help, --version, and the token-never-leaks security invariant."""

from __future__ import annotations

import httpx
import pytest
import respx
from typer.testing import CliRunner

from outo_models_cli.main import app

SERVER = "http://api.test"


@pytest.fixture
def runner() -> CliRunner:
    """Typer 0.27's `CliRunner` no longer accepts `mix_stderr` — stdout/stderr merge."""
    return CliRunner()


def test_root_help_lists_every_command(runner: CliRunner) -> None:
    """`omc --help` must surface auth, repo, ls, download, upload."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("auth", "repo", "ls", "download", "upload"):
        assert cmd in result.stdout


def test_auth_help_lists_every_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(app, ["auth", "--help"])
    assert result.exit_code == 0
    for sub in ("login", "logout", "whoami", "status"):
        assert sub in result.stdout


def test_repo_help_lists_every_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(app, ["repo", "--help"])
    assert result.exit_code == 0
    for sub in ("create", "delete", "list"):
        assert sub in result.stdout


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "omc" in result.stdout


def test_token_never_printed_anywhere(
    runner: CliRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Security: the token must never appear in stdout or stderr of any command."""
    secret = "TOPSECRET-9876543210"
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", secret])
    capsys.readouterr()  # discard the login output
    for cmd in (
        ["auth", "status"],
        ["auth", "whoami"],
        ["auth", "logout", "--server", SERVER],
    ):
        runner.invoke(app, cmd)
        captured = capsys.readouterr()
        assert secret not in captured.out, f"leaked in stdout: {cmd}"
        assert secret not in captured.err, f"leaked in stderr: {cmd}"


_ = httpx
