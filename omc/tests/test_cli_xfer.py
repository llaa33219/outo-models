"""End-to-end CLI tests for `omc ls` and `omc upload`."""

from __future__ import annotations

from pathlib import Path

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


def test_upload_routes_oversized_file_through_lfs(runner: CliRunner, tmp_path: Path) -> None:
    """An oversized file is routed through the LFS batch + PUT dance.

    The client must NOT pre-reject > 100 MiB files any more — that
    "must use git + LFS" wall was the field failure this whole change
    fixes. Now the CLI transparently partitions the set and issues the
    LFS batch call.
    """
    import respx as _respx

    _mock_me()
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])

    _respx.post(f"{SERVER}/alice/bert.git/info/lfs/objects/batch").mock(
        return_value=httpx.Response(
            200,
            json={"objects": []},
        )
    )

    big = tmp_path / "huge.bin"
    big.write_bytes(b"\x00" * 16)
    with big.open("wb") as fp:
        fp.truncate(101 * 1024 * 1024)  # 101 MiB
    respx.post(f"{SERVER}/api/repos/alice/bert/upload").respond(
        200,
        json={"commit_sha": "deadbeef", "files": ["huge.bin"], "message": None},
    )
    result = runner.invoke(app, ["upload", "alice/bert", str(big)])
    assert result.exit_code == 0, result.stderr
    assert "deadbeef" in result.stdout


def test_upload_missing_file_aborts(runner: CliRunner) -> None:
    _mock_me()
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", "t"])
    result = runner.invoke(app, ["upload", "alice/bert", "/no/such/path"])
    assert result.exit_code != 0
