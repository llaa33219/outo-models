"""Tests for the LFS mixed-commit helper.

The mixed commit is what `omc upload` issues after the LFS PUTs finish:
a single multipart call whose `files[]` part set mixes on-disk small
files with in-memory pointer-text buffers (one per large file path,
even when several paths share a deduped oid).
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import httpx

from outo_models_cli.api import lfs as lfs_api
from outo_models_cli.commands._upload_commit import commit_mixed

SERVER = "http://api.test"


class _BodyCapturingTransport(httpx.MockTransport):
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


def test_commit_mixed_includes_small_file_and_pointer(tmp_path: Path) -> None:
    """One multipart call carries both the small file bytes and the LFS pointer text."""
    small = tmp_path / "small.txt"
    small.write_bytes(b"hello world")
    big = tmp_path / "big.bin"
    payload = secrets.token_bytes(2 * 1024 * 1024)
    big.write_bytes(payload)
    oid = hashlib.sha256(payload).hexdigest()

    partition = lfs_api.partition_files([small, big], cap_bytes=1024)
    captured: dict[str, bytes] = {}
    response = httpx.Response(
        200,
        json={"commit_sha": "sha1", "files": ["small.txt", "big.bin"], "message": None},
    )
    from outo_models_cli import api

    transport = _BodyCapturingTransport(captured, response)
    with api.with_client(SERVER, "tok", transport=transport) as client:
        result = commit_mixed(
            client,
            owner="alice",
            name="bert",
            partition=partition,
            file_root=tmp_path,
            path_in_repo="",
            message=None,
        )
    assert result.commit_sha == "sha1"
    body = captured["body"]
    assert b"small.txt" in body
    assert b"hello world" in body
    assert b"big.bin" in body
    assert b"version https://git-lfs.github.com/spec/v1" in body
    assert b"oid sha256:" + oid.encode() in body


def test_commit_mixed_expands_dedup_to_one_part_per_path(tmp_path: Path) -> None:
    """Two paths with the same oid get two pointer parts, not one."""
    payload = secrets.token_bytes(1024)
    a = tmp_path / "a.bin"
    a.write_bytes(payload)
    b = tmp_path / "b.bin"
    b.write_bytes(payload)
    partition = lfs_api.partition_files([a, b], cap_bytes=512)
    assert len(partition.large) == 2
    assert partition.large[0].oid == partition.large[1].oid

    captured: dict[str, bytes] = {}
    response = httpx.Response(
        200,
        json={"commit_sha": "z", "files": ["a.bin", "b.bin"], "message": None},
    )
    from outo_models_cli import api

    transport = _BodyCapturingTransport(captured, response)
    with api.with_client(SERVER, "tok", transport=transport) as client:
        commit_mixed(
            client,
            owner="alice",
            name="bert",
            partition=partition,
            file_root=tmp_path,
            path_in_repo="",
            message=None,
        )

    body = captured["body"]
    assert body.count(b"a.bin") >= 1
    assert body.count(b"b.bin") >= 1
    pointer_marker = b"version https://git-lfs.github.com/spec/v1"
    assert body.count(pointer_marker) == 2


def test_commit_mixed_only_small_files(tmp_path: Path) -> None:
    """A pure-small partition still produces a valid multipart call."""
    a = tmp_path / "a.txt"
    a.write_bytes(b"alpha")
    b = tmp_path / "b.txt"
    b.write_bytes(b"beta")
    partition = lfs_api.partition_files([a, b], cap_bytes=1024)

    captured: dict[str, bytes] = {}
    response = httpx.Response(
        200,
        json={"commit_sha": "z", "files": ["a.txt", "b.txt"], "message": None},
    )
    from outo_models_cli import api

    transport = _BodyCapturingTransport(captured, response)
    with api.with_client(SERVER, "tok", transport=transport) as client:
        commit_mixed(
            client,
            owner="alice",
            name="bert",
            partition=partition,
            file_root=tmp_path,
            path_in_repo="",
            message=None,
        )

    body = captured["body"]
    assert b"alpha" in body
    assert b"beta" in body
    assert b"version https://git-lfs" not in body
