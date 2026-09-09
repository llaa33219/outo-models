"""LFS download redirect follow-through test.

The server's `/{owner}/{name}/resolve/{revision}/{path:path}` endpoint
returns a 302 to `/{owner}/{name}.git/info/lfs/objects/{oid}` whenever
the on-tree blob is an LFS pointer. The CLI's Basic-auth download
client must follow that redirect so the actual object bytes land on
disk rather than the 100-ish-byte pointer text.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from outo_models_cli.downloader import DownloadConfig, download_repo
from outo_models_cli.http import build_basic_async_client
from outo_models_cli.main import app

SERVER = "http://dl.test"
TOKEN = "pat-abc"
USERNAME = "alice"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_server() -> Iterator[_MockServer]:
    yield _MockServer()


class _MockServer:
    """Build `httpx.MockTransport` handlers for the LFS redirect test."""

    def build(self, handler: Any) -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return build_basic_async_client(SERVER, USERNAME, TOKEN, transport=transport)


def _files_response(entries: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"path": "", "entries": entries})


async def test_download_follows_lfs_redirect_to_object_bytes(
    tmp_path: Path, mock_server: _MockServer
) -> None:
    """A 302 from /resolve/... to /info/lfs/objects/{oid} returns the real bytes."""
    payload = secrets.token_bytes(2 * 1024 * 1024)  # 2 MiB
    oid = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.params.get("path", "")
        if request.url.path.endswith("/files"):
            if path == "":
                return _files_response(
                    [
                        {
                            "name": "big.bin",
                            "path": "big.bin",
                            "kind": "file",
                            "size_bytes": len(payload),
                        }
                    ]
                )
            return _files_response([])
        if request.url.path.endswith("/resolve/main/big.bin"):
            return httpx.Response(
                302,
                headers={"Location": f"/alice/bert.git/info/lfs/objects/{oid}"},
            )
        if request.url.path.endswith(f"/info/lfs/objects/{oid}"):
            headers = {"Content-Length": str(len(payload))}
            return httpx.Response(200, content=payload, headers=headers)
        return httpx.Response(404)

    cfg = DownloadConfig(revision="main", local_dir=tmp_path / "out", max_workers=1)
    async with mock_server.build(handler) as client:
        outcomes = await download_repo(
            client=client,
            base_url=SERVER,
            token=TOKEN,
            owner="alice",
            name="bert",
            config=cfg,
        )

    target = tmp_path / "out" / "big.bin"
    assert target.exists()
    assert target.read_bytes() == payload
    assert len(outcomes) == 1
    assert outcomes[0].bytes_downloaded == len(payload)


async def test_download_redirect_preserves_basic_auth(
    tmp_path: Path, mock_server: _MockServer
) -> None:
    """The redirect target sees the same Authorization the resolve request used."""
    captured: dict[str, str] = {}
    payload = b"hello"
    oid = "c" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.params.get("path", "")
        if request.url.path.endswith("/files"):
            if not path:
                return _files_response(
                    [
                        {
                            "name": "big.bin",
                            "path": "big.bin",
                            "kind": "file",
                            "size_bytes": len(payload),
                        }
                    ]
                )
            return _files_response([])
        if request.url.path.endswith("/resolve/main/big.bin"):
            return httpx.Response(
                302,
                headers={"Location": f"/alice/bert.git/info/lfs/objects/{oid}"},
            )
        if request.url.path.endswith(f"/info/lfs/objects/{oid}"):
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    cfg = DownloadConfig(revision="main", local_dir=tmp_path / "out", max_workers=1)
    async with mock_server.build(handler) as client:
        await download_repo(
            client=client,
            base_url=SERVER,
            token=TOKEN,
            owner="alice",
            name="bert",
            config=cfg,
        )
    assert captured["authorization"].startswith("Basic ")
    # Spot-check that the credentials match the test inputs.
    import base64

    decoded = base64.b64decode(captured["authorization"].split(" ", 1)[1]).decode()
    assert decoded == f"{USERNAME}:{TOKEN}"


def test_cli_download_handles_lfs_redirect(runner: CliRunner, tmp_path: Path) -> None:
    """The full CLI pipeline follows the LFS redirect and lands the real bytes."""
    import hashlib

    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": USERNAME, "role": "user"},
    )
    payload = secrets.token_bytes(3 * 1024 * 1024)  # 3 MiB
    oid = "b" * 64
    expected_digest = hashlib.sha256(payload).hexdigest()

    def files_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.params.get("path", "")
        if not path:
            return httpx.Response(
                200,
                json={
                    "path": "",
                    "entries": [
                        {
                            "name": "weights.bin",
                            "path": "weights.bin",
                            "kind": "file",
                            "size_bytes": len(payload),
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"path": path, "entries": []})

    def resolve_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": f"/alice/bert.git/info/lfs/objects/{oid}"},
        )

    def lfs_get_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Length": str(len(payload))},
        )

    respx.get(f"{SERVER}/api/repos/alice/bert/files").mock(side_effect=files_handler)
    respx.get(url__regex=r"/alice/bert/resolve/main/.*").mock(side_effect=resolve_handler)
    respx.get(url__regex=r"/alice/bert.git/info/lfs/objects/.*").mock(side_effect=lfs_get_handler)

    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", TOKEN])
    target = tmp_path / "downloaded"
    result = runner.invoke(app, ["download", "alice/bert", "--local-dir", str(target)])
    assert result.exit_code == 0, result.stderr

    out = target / "weights.bin"
    assert out.exists()
    assert hashlib.sha256(out.read_bytes()).hexdigest() == expected_digest
