"""End-to-end CLI tests for the `omc repo ...` sub-app."""

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


def test_repo_create_happy_path(runner: CliRunner) -> None:
    _mock_me()
    respx.post(f"{SERVER}/api/repos").respond(
        201,
        json={
            "name": "bert",
            "kind": "model",
            "visibility": "public",
            "description": "x",
            "size_bytes": 0,
            "owner": "alice",
            "clone_url": "https://x/alice/bert.git",
        },
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(
        app,
        ["repo", "create", "bert", "--kind", "model", "--public", "--description", "x"],
    )
    assert result.exit_code == 0, result.stderr
    assert "Created" in result.stdout
    assert "alice/bert" in result.stdout


def test_repo_create_403_clean_message(runner: CliRunner) -> None:
    _mock_me()
    respx.post(f"{SERVER}/api/repos").respond(403, json={"detail": "denied"})
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["repo", "create", "bert"])
    assert result.exit_code != 0
    assert "denied" in result.stderr


def test_repo_delete_confirms_then_calls(runner: CliRunner) -> None:
    _mock_me()
    delete = respx.delete(
        f"{SERVER}/api/repos/alice/bert",
        params={"kind": "model"},
    ).respond(204)
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["repo", "delete", "alice/bert", "--yes"])
    assert result.exit_code == 0, result.stderr
    assert delete.called


def test_repo_list_renders_table(runner: CliRunner) -> None:
    _mock_me()
    respx.get(f"{SERVER}/api/repos").respond(
        200,
        json=[
            {
                "name": "a",
                "kind": "model",
                "visibility": "public",
                "description": None,
                "size_bytes": 1024,
                "owner": "alice",
                "clone_url": "https://x/a.git",
            },
        ],
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["repo", "list"])
    assert result.exit_code == 0
    assert "a" in result.stdout
    assert "alice" in result.stdout


def test_repo_list_empty(runner: CliRunner) -> None:
    _mock_me()
    respx.get(f"{SERVER}/api/repos").respond(200, json=[])
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["repo", "list"])
    assert result.exit_code == 0
    assert "No repositories" in result.stdout


def test_repo_create_with_explicit_server_override(runner: CliRunner) -> None:
    other = "http://other.test"
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    respx.get(f"{other}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    respx.post(f"{other}/api/repos").respond(
        201,
        json={
            "name": "bert",
            "kind": "model",
            "visibility": "public",
            "description": None,
            "size_bytes": 0,
            "owner": "alice",
            "clone_url": "https://x/alice/bert.git",
        },
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    runner.invoke(app, ["auth", "login", "--server", other, "--token", "t"])
    result = runner.invoke(app, ["repo", "create", "bert", "--server", other])
    assert result.exit_code == 0, result.stderr


_ = httpx
