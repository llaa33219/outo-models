"""Unit tests for `outo_models.git_smart.lfs`.

The LFS handler is a v1 stub: every request returns `501 Not Implemented`
with a stable JSON envelope pointing operators at the documentation URL.
This file pins that contract so a future implementation can be wired in
without breaking wire compatibility.
"""

from __future__ import annotations

import json

import pytest


async def _noop_receive() -> dict[str, object]:
    """A no-op ASGI `receive` callable; lfs_not_supported never reads a body."""
    return {"type": "http.request", "body": b"", "more_body": False}


class TestLfsNotImplemented:
    """The stub always answers 501 + the documented JSON envelope."""

    async def test_returns_501(self) -> None:
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/alice/model.git/info/lfs/objects/batch",
        }
        await lfs_not_supported(scope, _noop_receive, send)

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
            {"type": "http", "method": "GET", "path": "/info/lfs/objects"},
            _noop_receive,
            send,
        )

        # Find the body message.
        bodies = [m for m in sent if m["type"] == "http.response.body"]
        assert len(bodies) == 1
        body = bodies[0].get("body")
        assert isinstance(body, (bytes, bytearray))
        payload = json.loads(bytes(body).decode("utf-8"))
        assert payload == {
            "error": "Git LFS is not supported yet",
            "docs": "/docs/git-repos",
        }

    async def test_sets_application_json_content_type(self) -> None:
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await lfs_not_supported(
            {"type": "http", "method": "POST", "path": "/info/lfs/objects/batch"},
            _noop_receive,
            send,
        )

        start = sent[0]
        assert start["type"] == "http.response.start"
        headers = start["headers"]
        assert headers is not None
        # Headers are list[tuple[bytes, bytes]] per ASGI spec.
        decoded = [(k.decode("ascii"), v.decode("ascii")) for k, v in headers]
        content_type = next((v for k, v in decoded if k.lower() == "content-type"), None)
        assert content_type is not None
        assert content_type.startswith("application/json")

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
    async def test_every_method_returns_501(self, method: str) -> None:
        """LFS clients pick verbs based on operation; the stub ignores them."""
        from outo_models.git_smart.lfs import lfs_not_supported

        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await lfs_not_supported(
            {"type": "http", "method": method, "path": "/info/lfs/objects"},
            _noop_receive,
            send,
        )

        assert sent[0]["status"] == 501
