"""End-to-end CLI tests for `omc ls` and `omc upload`."""

from __future__ import annotations

from pathlib import Path

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


def test_ls_renders_directory(runner: CliRunner) -> None:
    _mock_me()
    respx.get(f"{SERVER}/api/repos/alice/bert/files").respond(
        200,
        json={
            "path": "",
            "entries": [
                {"name": "README.md", "path": "README.md", "kind": "file", "size_bytes": 12},
                {"name": "src", "path": "src", "kind": "dir", "size_bytes": None},
            ],
        },
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["ls", "alice/bert"])
    assert result.exit_code == 0, result.stderr
    assert "README.md" in result.stdout
    assert "src" in result.stdout


def test_ls_bad_repo_format_aborts(runner: CliRunner) -> None:
    _mock_me()
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["ls", "no-slash"])
    assert result.exit_code != 0
    assert "owner" in result.stderr
    assert "<name>" in result.stderr or "name" in result.stderr


def test_upload_rejects_oversized_file(runner: CliRunner, tmp_path: Path) -> None:
    """The 100 MiB gate is enforced client-side BEFORE the upload is issued."""
    _mock_me()
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    big = tmp_path / "huge.bin"
    big.write_bytes(b"\x00" * 16)
    with big.open("wb") as fp:
        fp.truncate(101 * 1024 * 1024)  # 101 MiB
    result = runner.invoke(app, ["upload", "alice/bert", str(big)])
    assert result.exit_code != 0
    assert "100 MiB" in result.stderr or "LFS" in result.stderr


def test_upload_missing_file_aborts(runner: CliRunner) -> None:
    _mock_me()
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["upload", "alice/bert", "/no/such/path"])
    assert result.exit_code != 0
