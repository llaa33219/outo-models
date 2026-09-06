"""End-to-end tests for `download_repo` (orchestration + parallel downloads)."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from outo_models_cli.downloader import DownloadConfig, download_repo
from outo_models_cli.http import build_async_client

SERVER = "http://dl.test"
TOKEN = "pat-abc"


@pytest.fixture
def mock_server() -> Iterator[_MockServer]:
    yield _MockServer()


class _MockServer:
    """Build `httpx.MockTransport` handlers for the orchestration test."""

    def build(self, handler: Any) -> httpx.AsyncClient:
        return build_async_client(SERVER, TOKEN, transport=httpx.MockTransport(handler))


def _files_response(entries: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"path": "", "entries": entries})


async def test_download_repo_handles_three_files(tmp_path: Path, mock_server: _MockServer) -> None:
    """3-file repo → every file lands byte-exact in `<local_dir>/<path>`."""
    contents = {
        "a.txt": b"alpha " * 100,
        "b.bin": bytes(range(256)) * 4,
        "sub/c.txt": b"charlie",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.params.get("path", "")
        if request.url.path.endswith("/files"):
            if path == "":
                entries = [
                    {
                        "name": "a.txt",
                        "path": "a.txt",
                        "kind": "file",
                        "size_bytes": len(contents["a.txt"]),
                    },
                    {
                        "name": "b.bin",
                        "path": "b.bin",
                        "kind": "file",
                        "size_bytes": len(contents["b.bin"]),
                    },
                    {"name": "sub", "path": "sub", "kind": "dir", "size_bytes": None},
                ]
            elif path == "sub":
                entries = [
                    {
                        "name": "c.txt",
                        "path": "sub/c.txt",
                        "kind": "file",
                        "size_bytes": len(contents["sub/c.txt"]),
                    },
                ]
            else:
                entries = []
            return _files_response(entries)
        for key, body in contents.items():
            if request.url.path.endswith("/resolve/main/" + key):
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    cfg = DownloadConfig(revision="main", local_dir=tmp_path / "out", max_workers=2)
    async with mock_server.build(handler) as client:
        outcomes = await download_repo(
            client=client,
            base_url=SERVER,
            token=TOKEN,
            owner="alice",
            name="bert",
            config=cfg,
        )

    out_dir = tmp_path / "out"
    for path, body in contents.items():
        target = out_dir / path
        assert target.exists(), f"missing: {path}"
        assert target.read_bytes() == body, f"corrupt: {path}"
    assert len(outcomes) == 3
    assert all(not o.resumed for o in outcomes)


async def test_download_repo_resume_path(tmp_path: Path, mock_server: _MockServer) -> None:
    """Pre-existing .part → next run asks for Range → server replies 206."""
    full = b"x" * 1024
    existing = full[:300]
    out_dir = tmp_path / "out"
    (out_dir / "sub").mkdir(parents=True)
    part_path = out_dir / "sub" / "f.bin.part"
    part_path.write_bytes(existing)

    seen_range: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.params.get("path", "")
        if request.url.path.endswith("/files"):
            if path == "":
                return _files_response(
                    [
                        {"name": "sub", "path": "sub", "kind": "dir", "size_bytes": None},
                    ]
                )
            if path == "sub":
                return _files_response(
                    [
                        {
                            "name": "f.bin",
                            "path": "sub/f.bin",
                            "kind": "file",
                            "size_bytes": len(full),
                        },
                    ]
                )
            return _files_response([])
        if "/resolve/main/sub/f.bin" in request.url.path:
            seen_range["range"] = request.headers.get("range", "")
            if seen_range["range"]:
                start = int(seen_range["range"].split("=")[1].rstrip("-"))
                return httpx.Response(206, content=full[start:])
            return httpx.Response(200, content=full)
        return httpx.Response(404)

    cfg = DownloadConfig(revision="main", local_dir=out_dir, max_workers=1)
    async with mock_server.build(handler) as client:
        outcomes = await download_repo(
            client=client,
            base_url=SERVER,
            token=TOKEN,
            owner="alice",
            name="bert",
            config=cfg,
        )

    assert outcomes[0].resumed
    assert (out_dir / "sub" / "f.bin").read_bytes() == full
    assert seen_range["range"] == f"bytes={len(existing)}-"


async def test_download_repo_streams_64_mib_random_file(
    tmp_path: Path,
    mock_server: _MockServer,
) -> None:
    """A 64 MiB random payload downloads byte-exact through the streamer."""
    size = 64 * 1024 * 1024
    payload = secrets.token_bytes(size)
    digest = hashlib.sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return _files_response(
                [
                    {"name": "blob.bin", "path": "blob.bin", "kind": "file", "size_bytes": size},
                ]
            )
        if request.url.path.endswith("/resolve/main/blob.bin"):
            return httpx.Response(
                200,
                headers={"Content-Length": str(size)},
                content=payload,
            )
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

    target = tmp_path / "out" / "blob.bin"
    assert target.exists()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert outcomes[0].bytes_downloaded == size
