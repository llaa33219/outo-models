"""Unit tests for `outo_models.objectstore.LocalObjectStore`.

Pins the four contracts the local backend is responsible for:

    1. Round-trip: write → has → size → read → delete produces the
       same bytes that went in, and the file lands at the expected
       sharded path.
    2. Verification: a sha256 OR byte-count mismatch aborts the upload
       and leaves nothing on disk.
    3. Layout: objects sit under `<root>/<aa>/<bb>/<oid>`.
    4. Symlink safety: a planted symlink at the final slot, or at any
       sharding segment, is treated as missing — a write never follows
       a link and a download never reads through one.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from outo_models.exceptions import ValidationFailedError
from outo_models.objectstore import LfsAction, LocalObjectStore


def _oid(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def _aiter_bytes(data: bytes, chunk: int = 4096) -> AsyncIterator[bytes]:
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


def _store(tmp_path: Path) -> LocalObjectStore:
    root = tmp_path / "lfs"
    root.mkdir()
    return LocalObjectStore(root, base_url="http://lfs.test", presign_ttl=600)


class TestRoundTrip:
    """Write → exists → read returns the same bytes; delete then disappears."""

    async def test_write_read_has_size_delete_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = os.urandom(50_000)
        oid = _oid(payload)

        assert await store.has_object(oid) is False
        assert await store.object_size(oid) is None

        written = await store.write_object(oid, _aiter_bytes(payload), expected_size=len(payload))
        assert written == len(payload)
        assert await store.has_object(oid) is True
        assert await store.object_size(oid) == len(payload)

        # Read back — same bytes.
        collected = bytearray()
        async for chunk in store.read_object(oid):
            collected.extend(chunk)
        assert bytes(collected) == payload

        # Delete then disappears.
        await store.delete_object(oid)
        assert await store.has_object(oid) is False
        assert await store.object_size(oid) is None

    async def test_write_with_single_chunk(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = b"single chunk payload"
        oid = _oid(payload)
        written = await store.write_object(
            oid, _aiter_bytes(payload, chunk=len(payload)), expected_size=len(payload)
        )
        assert written == len(payload)
        assert await store.has_object(oid)

    async def test_empty_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        oid = hashlib.sha256(b"").hexdigest()
        written = await store.write_object(oid, _aiter_bytes(b""), expected_size=0)
        assert written == 0
        assert await store.has_object(oid)
        assert await store.object_size(oid) == 0


class TestVerificationFailures:
    """Mismatch conditions raise `ValidationFailedError` and leave no file."""

    async def test_sha256_mismatch_leaves_nothing_on_disk(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        real_payload = b"real bytes"
        # Lie about the oid — claim a different sha.
        bogus_oid = hashlib.sha256(b"different").hexdigest()

        with pytest.raises(ValidationFailedError):
            await store.write_object(
                bogus_oid, _aiter_bytes(real_payload), expected_size=len(real_payload)
            )

        assert await store.has_object(bogus_oid) is False
        # The final-path file must NOT exist; a sibling `.tmp` file must also be gone.
        target = store._object_path(bogus_oid)  # type: ignore[attr-defined]
        tmp = target.with_name(target.name + ".tmp")
        assert not target.exists()
        assert not tmp.exists()

    async def test_size_mismatch_leaves_nothing_on_disk(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = b"twelve byts"  # 11 bytes
        oid = _oid(payload)

        # Lie about expected_size: say 100, deliver 11.
        with pytest.raises(ValidationFailedError):
            await store.write_object(oid, _aiter_bytes(payload), expected_size=100)

        assert await store.has_object(oid) is False
        target = store._object_path(oid)  # type: ignore[attr-defined]
        assert not target.exists()

    async def test_rejects_existing_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = b"first write wins"
        oid = _oid(payload)
        await store.write_object(oid, _aiter_bytes(payload), expected_size=len(payload))

        with pytest.raises(ValidationFailedError):
            await store.write_object(oid, _aiter_bytes(payload), expected_size=len(payload))


class TestSharding:
    """The on-disk layout is `<root>/<aa>/<bb>/<oid>`."""

    async def test_object_lands_under_two_level_shard(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = b"shard me"
        oid = _oid(payload)
        await store.write_object(oid, _aiter_bytes(payload), expected_size=len(payload))

        target = store._object_path(oid)  # type: ignore[attr-defined]
        assert target.parent.name == oid[2:4]
        assert target.parent.parent.name == oid[:2]
        assert target.name == oid
        assert target.is_file()


class TestSymlinkSafety:
    """Symlinks are never followed — neither for reads nor for writes."""

    async def test_reading_through_symlink_at_final_path_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # Plant a real file outside the store, link the LFS slot at it.
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"secret bytes from outside the store")

        # Ensure parent sharding dirs exist so the symlink lands at the
        # exact final-path the store would resolve.
        target = store._object_path("0" * 64)  # type: ignore[attr-defined]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, target)

        # The store must refuse to serve the linked file.
        assert await store.has_object("0" * 64) is False
        assert await store.object_size("0" * 64) is None
        with pytest.raises(FileNotFoundError):
            async for _ in store.read_object("0" * 64):
                pass

    async def test_writing_through_symlink_at_shard_segment_is_rejected(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        # Plant a symlink at the first shard segment.
        outside = tmp_path / "outside_root"
        outside.mkdir()
        link_path = store._root / "aa"  # type: ignore[attr-defined]
        os.symlink(outside, link_path)

        payload = b"would-be-escape payload"
        # Use an oid that maps to /aa/bb/<oid>.
        oid = "aa" + "bb" + "0" * 60

        with pytest.raises(ValidationFailedError):
            await store.write_object(oid, _aiter_bytes(payload), expected_size=len(payload))

        # The escape directory must not have received the file.
        assert not (outside / "bb" / oid).exists()

    async def test_write_rejects_symlink_at_final_path(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        oid = "ff" + "ee" + "0" * 60
        target = store._object_path(oid)  # type: ignore[attr-defined]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(tmp_path / "elsewhere", target)

        with pytest.raises(ValidationFailedError):
            await store.write_object(oid, _aiter_bytes(b"x"), expected_size=1)


class TestAction:
    """`make_upload_action` and `make_download_action` build the same-origin href."""

    async def test_upload_and_download_actions_share_href(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        oid = "ab" + "cd" + "0" * 60
        upload = await store.make_upload_action(owner="alice", repo="model", oid=oid, size=42)
        download = await store.make_download_action(owner="alice", repo="model", oid=oid, size=42)

        assert isinstance(upload, LfsAction)
        assert upload.href == download.href
        assert upload.href == f"http://lfs.test/alice/model.git/info/lfs/objects/{oid}"
        assert upload.headers == {}
        assert upload.expires_in == 600

    async def test_strips_trailing_slash_from_base_url(self, tmp_path: Path) -> None:
        root = tmp_path / "lfs"
        root.mkdir()
        store = LocalObjectStore(root, base_url="http://lfs.test/", presign_ttl=600)
        action = await store.make_upload_action(owner="alice", repo="model", oid="0" * 64, size=0)
        assert action.href.startswith("http://lfs.test/")


class TestOidValidation:
    """Malformed oids fail before reaching the filesystem layer."""

    async def test_oid_too_short_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValidationFailedError):
            await store.write_object("abcd", _aiter_bytes(b"x"), expected_size=1)

    async def test_oid_non_hex_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        bad = "z" * 64
        with pytest.raises(ValidationFailedError):
            await store.write_object(bad, _aiter_bytes(b"x"), expected_size=1)

    async def test_oid_with_uppercase_hex_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        bad = "A" * 64
        with pytest.raises(ValidationFailedError):
            await store.write_object(bad, _aiter_bytes(b"x"), expected_size=1)
