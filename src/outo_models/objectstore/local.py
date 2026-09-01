"""Filesystem-backed LFS object store.

`LocalObjectStore` writes objects under `data_dir/lfs/<aa>/<bb>/<oid>` with
two-level sharding so no single directory accumulates more than a few
thousand entries. It implements the `ObjectStore` protocol plus two
server-side helpers (`write_object`, `read_object`) that the PUT/GET
handlers in `git_smart.lfs` use directly — the S3 backend will NOT use
those helpers; its href points at the S3 endpoint, so PUT/GET never
reach this store.

Upload safety:
    - Body is streamed to a sibling tmp file under the same parent dir,
      so `os.replace` becomes a single-filesystem rename and is atomic.
    - A sha256 mismatch OR a byte-count mismatch raises
      `ValidationFailedError` (HTTP 422) and the tmp file is removed.
    - Symlinks at any of the sharding segments OR at the final path are
      treated as missing — a planted symlink cannot satisfy a download
      nor shortcut a write.

Download streaming is a 64 KiB chunked async iteration over the on-disk
file. The chunk size matches what `git-lfs` reads off the wire so a
typical pull does not inflate into thousands of `send` calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import queue
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from outo_models.exceptions import ValidationFailedError
from outo_models.objectstore.base import LfsAction

#: Chunk size used when streaming objects back to the client.
_READ_CHUNK = 64 * 1024

#: Characters legal inside a git-lfs oid: lowercase hex, 64 chars long.
_HEX_CHARS = frozenset("0123456789abcdef")


def _validate_oid(oid: str) -> None:
    """Raise `ValidationFailedError` if `oid` is not a well-formed sha256 hex string.

    OIDs are exactly 64 lowercase hex chars per the LFS batch spec; we
    reject anything else at the boundary so a path-traversal attempt
    (e.g. `../../etc/passwd`) cannot reach the filesystem layer.
    """
    if not isinstance(oid, str) or len(oid) != 64:
        raise ValidationFailedError(f"invalid oid: not 64 chars: {oid!r}")
    if not all(c in _HEX_CHARS for c in oid.lower()):
        raise ValidationFailedError(f"invalid oid: non-hex characters: {oid!r}")


class LocalObjectStore:
    """Filesystem-backed LFS object store.

    Constructed with the root directory (typically `utils.paths.lfs_dir()`)
    plus the URL-building primitives pulled out of `Settings` so the
    store never imports `Settings` itself. The factory in
    `outo_models.objectstore.factory` is the single entry point that
    wires the right values in.
    """

    name: ClassVar[str] = "local"

    def __init__(
        self,
        root: Path,
        *,
        base_url: str,
        presign_ttl: int,
    ) -> None:
        self._root = root
        self._base_url = base_url.rstrip("/")
        self._presign_ttl = presign_ttl

    # ----- layout -----

    def _object_path(self, oid: str) -> Path:
        """Resolve the on-disk path for `oid`.

        Two-level sharding: `aa/bb/<oid>` where `aa`/`bb` are the first /
        second pair of hex chars. The sharded layout keeps any single
        directory small enough that `iterdir()` stays fast.
        """
        lower = oid.lower()
        return self._root / lower[:2] / lower[2:4] / lower

    def _action(self, *, owner: str, repo: str, oid: str) -> LfsAction:
        """Build the same-origin href the local backend hands to clients.

        git-lfs reuses the Basic credentials from the originating
        `git clone` / `git push` so no `Authorization` header is needed.
        `expires_in` reuses the S3 presign TTL purely as a generic
        "action lifetime" hint — the URL never actually expires.
        """
        href = f"{self._base_url}/{owner}/{repo}.git/info/lfs/objects/{oid}"
        return LfsAction(href=href, headers={}, expires_in=self._presign_ttl)

    # ----- ObjectStore protocol -----

    async def make_upload_action(
        self,
        *,
        owner: str,
        repo: str,
        oid: str,
        size: int,
    ) -> LfsAction:
        del size  # local backend does not sign size into the URL
        return self._action(owner=owner, repo=repo, oid=oid)

    async def make_download_action(
        self,
        *,
        owner: str,
        repo: str,
        oid: str,
        size: int,
    ) -> LfsAction:
        del size
        return self._action(owner=owner, repo=repo, oid=oid)

    async def has_object(self, oid: str) -> bool:
        path = self._object_path(oid)
        # `is_symlink()` short-circuits BEFORE `exists()` so a dangling
        # link does not register as a hit; `exists()` follows the link.
        if path.is_symlink():
            return False
        return path.exists()

    async def object_size(self, oid: str) -> int | None:
        path = self._object_path(oid)
        if path.is_symlink() or not path.exists():
            return None
        return path.stat().st_size

    async def delete_object(self, oid: str) -> None:
        path = self._object_path(oid)
        if path.is_symlink():
            # Defense in depth: never follow / remove a planted link.
            return
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(path.unlink)

    # ----- server-side helpers (local backend only) -----

    async def write_object(
        self,
        oid: str,
        body: AsyncIterator[bytes],
        expected_size: int,
    ) -> int:
        """Stream `body` into a tmp file; rename atomically on success.

        Returns the byte count actually written. On sha256 mismatch OR
        size mismatch, raises `ValidationFailedError` (HTTP 422) and the
        tmp file is removed — the final destination is never touched.

        Symlink rejection happens here too: if the final path (or any
        of its parents) is a symlink, we abort before reading a byte.
        """
        _validate_oid(oid)
        if expected_size < 0:
            raise ValidationFailedError(
                f"expected_size must be non-negative, got {expected_size}"
            )

        target = self._object_path(oid)
        if target.exists() or target.is_symlink():
            # Don't overwrite an existing object — concurrent upload race.
            raise ValidationFailedError(
                f"object already exists: {oid[:8]}"
            )

        # Parent dirs must exist; mkdir -p is idempotent.
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

        # Reject symlinks at any segment of the path the write would land on.
        for seg in (target.parent.parent, target.parent, target):
            if seg.is_symlink():
                raise ValidationFailedError(
                    f"refusing to write through symlink at {seg}"
                )

        tmp_path = target.with_name(target.name + ".tmp")
        if tmp_path.exists() or tmp_path.is_symlink():
            # Stale tmp from a previous failed write; scrub before retry.
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(tmp_path.unlink)

        hasher = hashlib.sha256()
        written = 0

        try:
            with tmp_path.open("wb") as fh:
                async for chunk in body:
                    if chunk:
                        fh.write(chunk)
                        hasher.update(chunk)
                        written += len(chunk)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise

        if written != expected_size:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise ValidationFailedError(
                f"size mismatch for {oid[:8]}: "
                f"expected {expected_size}, got {written}"
            )
        actual_oid = hasher.hexdigest()
        if actual_oid != oid:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise ValidationFailedError(
                f"sha256 mismatch: expected {oid}, got {actual_oid}"
            )

        # Atomic rename on the same filesystem; `os.replace` overwrites
        # atomically but we have already guaranteed the final slot is free.
        await asyncio.to_thread(os.replace, tmp_path, target)
        return written

    async def read_object(self, oid: str) -> AsyncIterator[bytes]:
        """Stream object bytes back to the client in 64 KiB chunks.

        Raises `FileNotFoundError` if the object is missing — the caller
        is responsible for converting that into a 404 response. Symlinks
        at the final path are refused up-front: a planted link cannot
        leak file contents outside the store.
        """
        _validate_oid(oid)
        path = self._object_path(oid)
        if path.is_symlink() or not path.exists():
            raise FileNotFoundError(oid)

        # Run the blocking file iteration in a worker thread that puts
        # chunks onto an unbounded queue (maxsize=0 ⇒ no back-pressure).
        # The producer must NOT block on `put`, otherwise we deadlock
        # with the consumer waiting on `to_thread` to return.
        loop = asyncio.get_running_loop()
        q: queue.Queue[bytes | BaseException | object] = queue.Queue()
        sentinel: object = object()

        def _pump() -> None:
            try:
                with path.open("rb") as fh:
                    while True:
                        buf = fh.read(_READ_CHUNK)
                        if not buf:
                            break
                        q.put(buf)
            except Exception as exc:  # surface to the async caller
                q.put(exc)
            finally:
                q.put(sentinel)

        # Schedule the producer; do NOT await its return — the producer
        # only completes when the file is fully read. We start the
        # consumer below so the two run concurrently.
        loop.run_in_executor(None, _pump)

        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                raise item
            assert isinstance(item, bytes)
            yield item


__all__ = ["LocalObjectStore"]