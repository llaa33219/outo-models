"""End-to-end tests for the LFS-aware `omc upload` command.

These tests run against the Typer `CliRunner` so the full command
pipeline (CLI parsing → config store → HTTP client → partition → LFS
batch → PUT → multipart commit) is exercised. Each test sets up a
fresh `respx` mock and asserts on the wire shape the server would see.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from outo_models_cli.main import app

SERVER = "http://api.test"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_me(username: str = "alice") -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": username, "role": "user"},
    )


def _login(runner: CliRunner, *, pat: str = "t", username: str = "alice") -> None:
    _mock_me(username=username)
    result = runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", pat])
    assert result.exit_code == 0, result.stderr


def test_upload_small_file_only_uses_multipart(runner: CliRunner, tmp_path: Path) -> None:
    """A 1-byte file skips the LFS path entirely and lands via multipart."""
    _login(runner)
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        chunks: list[bytes] = []
        try:
            for chunk in request.stream:
                if isinstance(chunk, bytes):
                    chunks.append(chunk)
                else:
                    for sub in chunk:
                        if isinstance(sub, bytes):
                            chunks.append(sub)
        except TypeError:
            pass
        captured["body"] = b"".join(chunks)
        return httpx.Response(200, json={"commit_sha": "cafe", "files": ["x.txt"], "message": None})

    respx.post(f"{SERVER}/api/repos/alice/bert/upload").mock(side_effect=handler)

    f = tmp_path / "x.txt"
    f.write_bytes(b"hi")
    result = runner.invoke(app, ["upload", "alice/bert", str(f)])
    assert result.exit_code == 0, result.stderr
    assert "cafe" in result.stdout
    body = captured["body"]
    assert b'name="files"' in body
    assert b"x.txt" in body


def test_upload_large_file_routes_through_lfs(runner: CliRunner, tmp_path: Path) -> None:
    """A 101 MiB file issues exactly one LFS batch + one PUT + one multipart commit."""
    _login(runner)
    cap = 100 * 1024 * 1024
    big = tmp_path / "weights.bin"
    payload = secrets.token_bytes(cap + 1024 * 1024)  # 101 MiB
    big.write_bytes(payload)
    expected_oid = hashlib.sha256(payload).hexdigest()

    batch_calls: list[dict[str, Any]] = []
    put_calls: list[dict[str, Any]] = []

    def batch_handler(request: httpx.Request) -> httpx.Response:
        batch_calls.append({"body": request.content, "headers": dict(request.headers)})
        return httpx.Response(
            200,
            json={
                "transfer": "basic",
                "objects": [
                    {
                        "oid": expected_oid,
                        "size": len(payload),
                        "actions": {
                            "upload": {
                                "href": f"{SERVER}/uploads/{expected_oid}",
                                "header": {},
                                "expires_in": 600,
                            }
                        },
                    }
                ],
            },
        )

    def put_handler(request: httpx.Request) -> httpx.Response:
        body_chunks: list[bytes] = []
        for chunk in request.stream:
            if isinstance(chunk, bytes):
                body_chunks.append(chunk)
            else:
                for sub in chunk:
                    if isinstance(sub, bytes):
                        body_chunks.append(sub)
        body = b"".join(body_chunks)
        digest = hashlib.sha256(body).hexdigest()
        put_calls.append({"url": str(request.url), "length": len(body), "digest": digest})
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{SERVER}/alice/bert.git/info/lfs/objects/batch").mock(side_effect=batch_handler)
    respx.put(url__regex=r".*/uploads/.*").mock(side_effect=put_handler)

    commit_captured: dict[str, bytes] = {}

    def commit_handler(request: httpx.Request) -> httpx.Response:
        chunks: list[bytes] = []
        try:
            for chunk in request.stream:
                if isinstance(chunk, bytes):
                    chunks.append(chunk)
                else:
                    for sub in chunk:
                        if isinstance(sub, bytes):
                            chunks.append(sub)
        except TypeError:
            pass
        commit_captured["body"] = b"".join(chunks)
        return httpx.Response(
            200,
            json={"commit_sha": "deadbeef", "files": ["weights.bin"], "message": None},
        )

    respx.post(f"{SERVER}/api/repos/alice/bert/upload").mock(side_effect=commit_handler)

    result = runner.invoke(app, ["upload", "alice/bert", str(big)])
    assert result.exit_code == 0, result.stderr
    assert "deadbeef" in result.stdout

    assert len(batch_calls) == 1
    body = batch_calls[0]["body"]
    assert b'"operation": "upload"' in body or b'"operation":"upload"' in body
    assert expected_oid.encode() in body

    assert len(put_calls) == 1
    put = put_calls[0]
    assert put["length"] == len(payload)
    assert put["digest"] == expected_oid

    body = commit_captured["body"]
    assert b"weights.bin" in body
    assert b"oid sha256:" + expected_oid.encode() in body
    assert b"version https://git-lfs.github.com/spec/v1" in body


def test_upload_mixed_small_and_large_lands_one_multipart(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A small file + a large file share ONE multipart commit."""
    _login(runner)
    cap = 100 * 1024 * 1024
    big = tmp_path / "weights.bin"
    big_payload = secrets.token_bytes(cap + 1024 * 1024)
    big.write_bytes(big_payload)
    big_oid = hashlib.sha256(big_payload).hexdigest()

    small = tmp_path / "README.md"
    small.write_bytes(b"# hello")

    respx.post(f"{SERVER}/alice/bert.git/info/lfs/objects/batch").respond(
        200,
        json={
            "transfer": "basic",
            "objects": [
                {
                    "oid": big_oid,
                    "size": len(big_payload),
                    "actions": {
                        "upload": {
                            "href": f"{SERVER}/uploads/{big_oid}",
                            "header": {},
                            "expires_in": 600,
                        }
                    },
                }
            ],
        },
    )
    respx.put(url__regex=r".*/uploads/.*").respond(200, json={"ok": True})

    commit_captured: dict[str, bytes] = {}

    def commit_handler(request: httpx.Request) -> httpx.Response:
        chunks: list[bytes] = []
        try:
            for chunk in request.stream:
                if isinstance(chunk, bytes):
                    chunks.append(chunk)
                else:
                    for sub in chunk:
                        if isinstance(sub, bytes):
                            chunks.append(sub)
        except TypeError:
            pass
        commit_captured["body"] = b"".join(chunks)
        return httpx.Response(
            200,
            json={"commit_sha": "abc", "files": ["README.md", "weights.bin"], "message": None},
        )

    respx.post(f"{SERVER}/api/repos/alice/bert/upload").mock(side_effect=commit_handler)

    result = runner.invoke(app, ["upload", "alice/bert", str(tmp_path)])
    assert result.exit_code == 0, result.stderr

    body = commit_captured["body"]
    assert b"README.md" in body
    assert b"weights.bin" in body
    assert b"# hello" in body
    assert b"version https://git-lfs.github.com/spec/v1" in body
    assert b"oid sha256:" + big_oid.encode() in body


def test_upload_already_present_object_skips_put(runner: CliRunner, tmp_path: Path) -> None:
    """An object the server already has → no PUT → pointer still committed."""
    _login(runner)
    cap = 100 * 1024 * 1024
    big = tmp_path / "weights.bin"
    payload = secrets.token_bytes(cap + 1024 * 1024)
    big.write_bytes(payload)
    expected_oid = hashlib.sha256(payload).hexdigest()

    respx.post(f"{SERVER}/alice/bert.git/info/lfs/objects/batch").respond(
        200,
        json={
            "transfer": "basic",
            "objects": [{"oid": expected_oid, "size": len(payload)}],
        },
    )
    put_route = respx.put(url__regex=r".*/uploads/.*").respond(200, json={"ok": True})

    commit_captured: dict[str, bytes] = {}

    def commit_handler(request: httpx.Request) -> httpx.Response:
        chunks: list[bytes] = []
        try:
            for chunk in request.stream:
                if isinstance(chunk, bytes):
                    chunks.append(chunk)
                else:
                    for sub in chunk:
                        if isinstance(sub, bytes):
                            chunks.append(sub)
        except TypeError:
            pass
        commit_captured["body"] = b"".join(chunks)
        return httpx.Response(
            200,
            json={"commit_sha": "ok", "files": ["weights.bin"], "message": None},
        )

    respx.post(f"{SERVER}/api/repos/alice/bert/upload").mock(side_effect=commit_handler)

    result = runner.invoke(app, ["upload", "alice/bert", str(big)])
    assert result.exit_code == 0, result.stderr
    assert not put_route.called
    assert b"oid sha256:" + expected_oid.encode() in commit_captured["body"]


def test_upload_413_error_lists_failing_file(runner: CliRunner, tmp_path: Path) -> None:
    """A per-object 413 surfaces the file name and aborts the upload."""
    _login(runner)
    cap = 100 * 1024 * 1024
    big = tmp_path / "too-big.bin"
    payload = secrets.token_bytes(cap + 1024 * 1024)
    big.write_bytes(payload)
    oid = hashlib.sha256(payload).hexdigest()

    respx.post(f"{SERVER}/alice/bert.git/info/lfs/objects/batch").respond(
        200,
        json={
            "transfer": "basic",
            "objects": [
                {
                    "oid": oid,
                    "size": len(payload),
                    "error": {
                        "code": 413,
                        "message": "object exceeds per-object limit",
                    },
                }
            ],
        },
    )

    result = runner.invoke(app, ["upload", "alice/bert", str(big)])
    assert result.exit_code != 0
    assert "too-big.bin" in result.stderr
    assert "413" in result.stderr
