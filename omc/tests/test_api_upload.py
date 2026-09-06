"""Tests for the upload API: multipart shape, subpath, size limit, error mapping."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from outo_models_cli import api
from outo_models_cli.commands._shared import _MAX_FILE_BYTES, _check_size_limit
from outo_models_cli.errors import BadResponseError, FileTooLargeError

SERVER = "http://api.test"
TOKEN = "pat-abc"


class _BodyCapturingTransport(httpx.MockTransport):
    """A `MockTransport` subclass that captures the raw request body.

    httpx's multipart stream emits a sequence of `(bytes, ...)` tuples
    rather than plain bytes, so `request.content` raises `TypeError` when
    the request stream hasn't been consumed. `MockTransport.handle_request`
    is the lowest-level hook that sees the request exactly once, so we
    flatten the stream there.
    """

    def __init__(self, body_holder: dict[str, bytes], response: httpx.Response) -> None:
        self._body_holder = body_holder
        self._response = response
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
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
        self._body_holder["body"] = b"".join(chunks)
        return self._response


def test_upload_happy_path(tmp_path: Any) -> None:
    """A file upload with subpath preservation round-trips and acks the commit."""
    file = tmp_path / "weights.bin"
    file.write_bytes(b"\x00\x01\x02")

    response = httpx.Response(
        200,
        json={"commit_sha": "deadbeef", "files": ["weights.bin"]},
    )
    captured: dict[str, bytes] = {}
    transport = _BodyCapturingTransport(captured, response)

    with api.with_client(SERVER, TOKEN, transport=transport) as client:
        result = api.upload(
            client,
            owner="alice",
            name="bert",
            files=[file],
            path_in_repo="weights",
            message="v1",
        )

    assert result.commit_sha == "deadbeef"


def test_upload_multipart_has_files_and_path(tmp_path: Any) -> None:
    """The multipart body must carry `files` and the `path` field (server contract)."""
    file = tmp_path / "a.txt"
    file.write_bytes(b"hello")

    response = httpx.Response(
        200,
        json={"commit_sha": "abc", "files": ["a.txt"]},
    )
    captured: dict[str, bytes] = {}
    transport = _BodyCapturingTransport(captured, response)

    with api.with_client(SERVER, TOKEN, transport=transport) as client:
        api.upload(
            client,
            owner="alice",
            name="bert",
            files=[file],
            path_in_repo="subdir",
            message="m",
        )

    body = captured.get("body", b"")
    assert b'name="files"' in body
    assert b"a.txt" in body
    assert b"subdir" in body
    assert b"path" in body
    assert b"m" in body


def test_upload_subpath_preserved(tmp_path: Any) -> None:
    """When `path_in_repo` is set, every file lands under it."""
    a = tmp_path / "a.bin"
    a.write_bytes(b"a")
    b = tmp_path / "b.bin"
    b.write_bytes(b"b")

    response = httpx.Response(200, json={"commit_sha": "x", "files": ["a.bin", "b.bin"]})
    captured: dict[str, bytes] = {}
    transport = _BodyCapturingTransport(captured, response)

    with api.with_client(SERVER, TOKEN, transport=transport) as client:
        result = api.upload(
            client,
            owner="alice",
            name="bert",
            files=[a, b],
            path_in_repo="weights",
            message=None,
        )
    assert len(result.files) == 2
    body = captured.get("body", b"")
    assert b'Content-Disposition: form-data; name="path"' in body
    assert b"\r\n\r\nweights\r\n" in body


def test_upload_rejects_oversized_file_client_side(tmp_path: Any) -> None:
    """`omc upload` is responsible for the 100 MiB gate — not the server."""
    file = tmp_path / "big.bin"
    file.write_bytes(b"\x00" * 16)
    with file.open("wb") as fp:
        fp.truncate(_MAX_FILE_BYTES + 1)

    with pytest.raises(FileTooLargeError):
        _check_size_limit([file])


def test_upload_413_maps_to_file_too_large(tmp_path: Any) -> None:
    """Server-side 413 must surface as `FileTooLargeError` (not generic 4xx)."""
    file = tmp_path / "x.bin"
    file.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"detail": "quota"})

    with (
        api.with_client(SERVER, TOKEN, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(FileTooLargeError),
    ):
        api.upload(
            client,
            owner="alice",
            name="bert",
            files=[file],
            path_in_repo="",
        )


def test_upload_bad_response_on_missing_commit_sha(tmp_path: Any) -> None:
    """A 200 with no commit_sha is a server bug — `BadResponseError`."""
    file = tmp_path / "x.bin"
    file.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": []})

    with (
        api.with_client(SERVER, TOKEN, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BadResponseError),
    ):
        api.upload(
            client,
            owner="alice",
            name="bert",
            files=[file],
            path_in_repo="",
        )
