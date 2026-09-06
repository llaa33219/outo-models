"""Tests for single-file streaming: full download, Range resume, ETag mismatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from outo_models_cli.api import FileEntry
from outo_models_cli.downloader.transfer import FileOutcome, transfer_one
from outo_models_cli.http import build_async_client

SERVER = "http://dl.test"
TOKEN = "pat-abc"


class _StubProgress:
    """Minimal stand-in for `rich.progress.Progress` (we only call `update`)."""

    def update(self, *args: Any, **kwargs: Any) -> None:
        return None


async def _transfer(target: Path, handler: Any) -> FileOutcome:
    """Build a minimal `transfer_one` setup with a stub Progress."""
    async with build_async_client(SERVER, TOKEN, transport=httpx.MockTransport(handler)) as client:
        entry = FileEntry(
            name=target.name,
            path=target.name,
            kind="file",
            size_bytes=None,
        )
        return await transfer_one(
            client,
            owner="alice",
            name="bert",
            revision="main",
            file=entry,
            target=target,
            progress=_StubProgress(),  # type: ignore[arg-type]
            task_id=None,
        )


async def test_transfer_one_streams_full_file(tmp_path: Path) -> None:
    """A fresh download writes the whole file via `.part` then renames."""
    body = b"abcdefghij" * 100
    target = tmp_path / "out.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"ETag": '"abc"', "Content-Length": str(len(body))},
            content=body,
        )

    outcome = await _transfer(target, handler)
    assert outcome.bytes_downloaded == len(body)
    assert not outcome.resumed
    assert target.exists()
    assert target.read_bytes() == body
    assert not target.with_suffix(target.suffix + ".part").exists()


async def test_transfer_one_resumes_with_range_header(tmp_path: Path) -> None:
    """A pre-existing `.part` triggers a Range request and appends."""
    target = tmp_path / "out.bin"
    part_path = target.with_suffix(target.suffix + ".part")
    existing = b"abcdefghij" * 50  # 500 bytes
    remainder = b"klmnopqrst" * 50  # another 500 bytes
    part_path.write_bytes(existing)

    captured_range: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_range["range"] = request.headers.get("range", "")
        return httpx.Response(
            206,
            headers={"ETag": '"abc"', "Content-Length": str(len(remainder))},
            content=remainder,
        )

    outcome = await _transfer(target, handler)
    assert captured_range["range"] == f"bytes={len(existing)}-"
    assert outcome.resumed
    assert outcome.bytes_downloaded == len(existing) + len(remainder)
    assert target.read_bytes() == existing + remainder
    assert not part_path.exists()


async def test_transfer_one_etag_mismatch_triggers_full_download(tmp_path: Path) -> None:
    """A `.part` whose ETag no longer matches → server returns 200 → full redownload."""
    target = tmp_path / "out.bin"
    part_path = target.with_suffix(target.suffix + ".part")
    stale = b"OLD-DATA"
    full = b"NEW-DATA" * 200
    part_path.write_bytes(stale)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range", ""))
        return httpx.Response(
            200,
            headers={"ETag": '"new"', "Content-Length": str(len(full))},
            content=full,
        )

    outcome = await _transfer(target, handler)
    assert len(calls) == 1
    assert calls[0] == f"bytes={len(stale)}-"
    assert not outcome.resumed
    assert target.read_bytes() == full
    assert not part_path.exists()


async def test_transfer_one_416_triggers_restart(tmp_path: Path) -> None:
    """416 → .part is corrupt or server's file shrunk → start over."""
    target = tmp_path / "out.bin"
    part_path = target.with_suffix(target.suffix + ".part")
    part_path.write_bytes(b"stale")

    full = b"actual-content"
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(416)
        return httpx.Response(200, content=full)

    await _transfer(target, handler)
    assert call_count["n"] == 2
    assert target.read_bytes() == full
