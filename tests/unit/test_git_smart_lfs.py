"""Unit tests for the locks-only 501 stub.

Git LFS is a stub for everything EXCEPT the batch / objects endpoints —
the only `*.git/info/lfs/*` request that still answers `501` is anything
under `/info/lfs/locks`. Every other LFS verb is served by the real
dispatcher in `outo_models.git_smart.lfs.lfs_dispatch`.

This file pins the wire contract of the locks 501: the response carries
a stable JSON envelope pointing operators at the documentation URL so
a future removal can be detected by monitoring.
"""

from __future__ import annotations

import json

import pytest


async def _noop_receive() -> dict[str, object]:
    """A no-op ASGI `receive` callable; the locks handler never reads a body."""
    return {"type": "http.request", "body": b"", "more_body": False}


class TestLocks501:
    """Every method under `/info/lfs/locks*` returns 501 + docs link."""

    async def test_returns_501_for_locks_path(self) -> None:
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await lfs_not_supported(
            {"type": "http", "method": "GET", "path": "/alice/model.git/info/lfs/locks"},
            _noop_receive,
            send,
        )

        assert sent, "handler must emit at least one ASGI message"
        start = sent[0]
        assert start["type"] == "http.response.start"
        assert start["status"] == 501

    async def test_emits_documented_json_envelope(self) -> None:
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await lfs_not_supported(
            {"type": "http", "method": "GET", "path": "/info/lfs/locks"},
            _noop_receive,
            send,
        )

        bodies = [m for m in sent if m["type"] == "http.response.body"]
        assert len(bodies) == 1
        body = bodies[0].get("body")
        assert isinstance(body, (bytes, bytearray))
        payload = json.loads(bytes(body).decode("utf-8"))
        assert payload["docs"] == "/docs/git-lfs"
        assert "locks" in payload["error"].lower()

    async def test_sets_application_json_content_type(self) -> None:
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await lfs_not_supported(
            {"type": "http", "method": "POST", "path": "/info/lfs/locks/verify"},
            _noop_receive,
            send,
        )

        start = sent[0]
        headers = start["headers"]
        assert headers is not None
        decoded = [(k.decode("ascii"), v.decode("ascii")) for k, v in headers]
        content_type = next((v for k, v in decoded if k.lower() == "content-type"), None)
        assert content_type is not None
        assert content_type.startswith("application/json")

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
    async def test_every_method_returns_501(self, method: str) -> None:
        """Locks clients pick verbs based on operation; the handler ignores them."""
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await lfs_not_supported(
            {"type": "http", "method": method, "path": "/info/lfs/locks"},
            _noop_receive,
            send,
        )

        assert sent[0]["status"] == 501