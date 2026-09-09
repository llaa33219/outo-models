"""Tests for the LFS module: partition, pointer text, batch wire shape, dedup."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from outo_models_cli import api
from outo_models_cli.api import lfs as lfs_api
from outo_models_cli.errors import BadResponseError

SERVER = "http://api.test"


def test_pointer_text_byte_exact() -> None:
    """Pointer text matches the wire contract the server sniffs for."""
    text = lfs_api.pointer_text("a" * 64, 12345).decode("ascii")
    expected = (
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 12345\n"
    )
    assert text == expected


def test_partition_files_splits_by_cap_and_hashes_large(tmp_path: Path) -> None:
    """A mixed set yields a small bucket and a large bucket with sha256s."""
    cap = 1024  # 1 KiB threshold keeps the test fast.
    small = tmp_path / "small.bin"
    small.write_bytes(b"a" * 500)
    large = tmp_path / "large.bin"
    payload = b"x" * (cap * 2)
    large.write_bytes(payload)

    result = lfs_api.partition_files([small, large], cap_bytes=cap)
    assert result.small == [small]
    assert len(result.large) == 1
    item = result.large[0]
    assert item.path == large
    assert item.size == cap * 2
    assert item.oid == hashlib.sha256(payload).hexdigest()


def test_partition_files_handles_empty_input() -> None:
    """`partition_files([])` returns empty buckets (no streaming, no errors)."""
    result = lfs_api.partition_files([], cap_bytes=1)
    assert result.small == []
    assert result.large == []


def test_dedupe_objects_collapses_identical_oids() -> None:
    """Two paths with identical bytes collapse to one batch entry."""
    cap = 10
    a = lfs_api.LargeFile(path=Path("/tmp/a.bin"), oid="z" * 64, size=cap * 4)
    b = lfs_api.LargeFile(path=Path("/tmp/b.bin"), oid="z" * 64, size=cap * 4)
    c = lfs_api.LargeFile(path=Path("/tmp/c.bin"), oid="y" * 64, size=cap * 5)
    entries, maps = lfs_api.dedupe_objects([a, b, c])
    assert entries == [
        {"oid": "z" * 64, "size": cap * 4},
        {"oid": "y" * 64, "size": cap * 5},
    ]
    by_oid = {m.key[0]: m.paths for m in maps}
    assert set(by_oid["z" * 64]) == {Path("/tmp/a.bin"), Path("/tmp/b.bin")}
    assert by_oid["y" * 64] == (Path("/tmp/c.bin"),)


def test_pointers_for_dedupes_by_oid() -> None:
    """Identical-oid files share a single pointer text payload."""
    items = [
        lfs_api.LargeFile(path=Path("/tmp/a.bin"), oid="k" * 64, size=11),
        lfs_api.LargeFile(path=Path("/tmp/b.bin"), oid="k" * 64, size=11),
    ]
    pointers = lfs_api.pointers_for(items)
    assert len(pointers) == 1
    assert b"size 11" in pointers["k" * 64]


def test_batch_upload_sends_required_headers() -> None:
    """The batch request MUST carry both `Accept` and `Content-Type` LFS headers."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("accept", "")
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "transfer": "basic",
                "objects": [
                    {
                        "oid": "z" * 64,
                        "size": 1024,
                        "actions": {
                            "upload": {
                                "href": "http://api.test/upload",
                                "header": {},
                                "expires_in": 600,
                            }
                        },
                    }
                ],
            },
        )

    with api.with_client(SERVER, "tok", transport=httpx.MockTransport(handler)) as client:
        actions, errors, present = api.batch_upload(
            client,
            owner="alice",
            name="bert",
            objects=[{"oid": "z" * 64, "size": 1024}],
        )
    assert captured["accept"] == api.LFS_CONTENT_TYPE
    assert captured["content_type"] == api.LFS_CONTENT_TYPE
    assert "operation" in captured["body"]
    assert actions and actions[0].href == "http://api.test/upload"
    assert not errors
    assert not present


def test_batch_upload_present_object_returns_no_action() -> None:
    """An object the server already has comes back without `actions`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transfer": "basic",
                "objects": [{"oid": "p" * 64, "size": 7}],
            },
        )

    with api.with_client(SERVER, "tok", transport=httpx.MockTransport(handler)) as client:
        actions, errors, _present = api.batch_upload(
            client,
            owner="alice",
            name="bert",
            objects=[{"oid": "p" * 64, "size": 7}],
        )
    assert actions == []
    assert errors == []
    assert _present == ["p" * 64]


def test_batch_upload_per_object_error_surfaces_in_errors_list() -> None:
    """A 413 for one object must NOT fail the whole batch."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transfer": "basic",
                "objects": [
                    {
                        "oid": "a" * 64,
                        "size": 10,
                        "actions": {
                            "upload": {
                                "href": "http://api.test/upload-a",
                                "header": {},
                                "expires_in": 600,
                            }
                        },
                    },
                    {
                        "oid": "b" * 64,
                        "size": 999999,
                        "error": {
                            "code": 413,
                            "message": "object size exceeds per-object limit",
                        },
                    },
                ],
            },
        )

    with api.with_client(SERVER, "tok", transport=httpx.MockTransport(handler)) as client:
        actions, errors, _present = api.batch_upload(
            client,
            owner="alice",
            name="bert",
            objects=[
                {"oid": "a" * 64, "size": 10},
                {"oid": "b" * 64, "size": 999999},
            ],
        )
    assert [a.oid for a in actions] == ["a" * 64]
    assert len(errors) == 1
    assert errors[0].oid == "b" * 64
    assert errors[0].code == 413


def test_batch_upload_404_raises_clean_error() -> None:
    """Older server without LFS → 404 → clean English line, not a stack."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with (
        api.with_client(SERVER, "tok", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BadResponseError) as exc_info,
    ):
        api.batch_upload(
            client,
            owner="alice",
            name="bert",
            objects=[{"oid": "q" * 64, "size": 1}],
        )
    assert "LFS" in str(exc_info.value)


def test_batch_upload_propagates_action_headers(tmp_path: Path) -> None:
    """The action `header` map MUST be forwarded verbatim to the PUT."""
    captured: dict[str, str] = {}
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["authorization"] = request.headers.get("authorization", "")
            captured["content_length"] = request.headers.get("content-length", "")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            json={
                "transfer": "basic",
                "objects": [
                    {
                        "oid": "z" * 64,
                        "size": 5,
                        "actions": {
                            "upload": {
                                "href": "http://api.test/upload-hdr",
                                "header": {"Authorization": "Bearer presigned"},
                                "expires_in": 60,
                            }
                        },
                    }
                ],
            },
        )

    with api.with_client(SERVER, "tok", transport=httpx.MockTransport(handler)) as client:
        actions, errors, present = api.batch_upload(
            client,
            owner="alice",
            name="bert",
            objects=[{"oid": "z" * 64, "size": 5}],
        )
        assert not errors and not present
        api.upload_objects(
            client,
            actions=actions,
            paths_by_oid={"z" * 64: [src]},
            show_progress=False,
        )

    assert captured["authorization"] == "Bearer presigned"
    assert captured["content_length"] == "5"
