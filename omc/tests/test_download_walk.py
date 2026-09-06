"""Tests for the recursive tree walker."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from outo_models_cli.downloader.walk import list_files_async, walk_repo_async
from outo_models_cli.http import build_async_client

SERVER = "http://dl.test"
TOKEN = "pat-abc"


@pytest.fixture
def mock_server() -> Iterator[_MockServer]:
    yield _MockServer()


class _MockServer:
    """Build `httpx.MockTransport` handlers for the tree walker."""

    def build(self, handler: Any) -> httpx.AsyncClient:
        return build_async_client(SERVER, TOKEN, transport=httpx.MockTransport(handler))


def _files_response(entries: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"path": "", "entries": entries})


async def test_walk_repo_recurses_into_directories(mock_server: _MockServer) -> None:
    """Tree walk returns a flat list of every file, depth-first."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/repos/alice/bert/files" and not request.url.params.get("path"):
            return _files_response(
                [
                    {"name": "top.txt", "path": "top.txt", "kind": "file", "size_bytes": 3},
                    {"name": "sub", "path": "sub", "kind": "dir", "size_bytes": None},
                ]
            )
        if (
            request.url.path == "/api/repos/alice/bert/files"
            and request.url.params.get("path") == "sub"
        ):
            return _files_response(
                [
                    {"name": "deep.bin", "path": "sub/deep.bin", "kind": "file", "size_bytes": 7},
                ]
            )
        return httpx.Response(404)

    async with mock_server.build(handler) as client:
        files = await walk_repo_async(
            client,
            owner="alice",
            name="bert",
            revision="main",
            include=[],
            exclude=[],
        )
    paths = [f.path for f in files]
    assert paths == ["top.txt", "sub/deep.bin"]


async def test_walk_repo_applies_include_and_exclude(mock_server: _MockServer) -> None:
    """A filter list reduces the walked set, never enlarges it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _files_response(
            [
                {"name": "a.bin", "path": "a.bin", "kind": "file", "size_bytes": 1},
                {"name": "a.safetensors", "path": "a.safetensors", "kind": "file", "size_bytes": 1},
                {"name": "b.txt", "path": "b.txt", "kind": "file", "size_bytes": 1},
            ]
        )

    async with mock_server.build(handler) as client:
        files = await walk_repo_async(
            client,
            owner="alice",
            name="bert",
            revision="main",
            include=["*.safetensors", "*.bin"],
            exclude=["a.bin"],
        )
    assert [f.path for f in files] == ["a.safetensors"]


async def test_list_files_forwards_revision(mock_server: _MockServer) -> None:
    """`?revision=` is forwarded verbatim (server may ignore)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("revision", ""))
        return _files_response([])

    async with mock_server.build(handler) as client:
        await list_files_async(
            client,
            owner="alice",
            name="bert",
            path="",
            revision="dev",
        )
    assert calls == ["dev"]
