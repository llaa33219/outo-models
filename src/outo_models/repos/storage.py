"""Filesystem layout helpers + per-repo async lock registry.

Everything the rest of `outo_models.repos` needs that touches the disk or
serializes concurrent writers lives here. The module is small on purpose:
`create`, `delete`, and `reflog` all build on top of these primitives and
should not duplicate them.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from outo_models.utils.paths import repo_path as _repo_path


def repo_fs_path(owner: str, name: str) -> Path:
    """Return the absolute on-disk path of a bare repository.

    Thin pass-through over `utils.paths.repo_path`; the wrapper exists so the
    rest of `repos` does not import from `utils` directly (and so a future
    rewrite that adds caching or normalization has a single seam).
    """
    return _repo_path(owner, name)


def repo_exists(owner: str, name: str) -> bool:
    """Return True iff a bare repository already lives at the owner's slot.

    Existence is decided by the bare repo directory itself being present; we
    do not require a `HEAD` ref because freshly initialized bare repos carry
    only a symbolic HEAD until the first push.
    """
    return repo_fs_path(owner, name).exists()


def _walk_disk_usage(path: Path) -> int:
    """Recursively sum file sizes under `path`; never follows symlinks.

    A symlink at any depth is skipped — including the top-level path — so a
    malicious or accidental symlink in `data_dir` cannot be used to read
    arbitrary files or inflate the apparent size of a repo.
    """
    if not path.exists() or path.is_symlink():
        return 0
    total = 0
    for entry in os.scandir(path):
        # `os.DirEntry.is_symlink` uses `os.lstat` semantics, so it does not
        # resolve the link target. Following-symlinks for nested directories
        # is also disabled below via `follow_symlinks=False`.
        if entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            total += _walk_disk_usage(Path(entry.path))
        else:
            total += entry.stat(follow_symlinks=False).st_size
    return total


async def disk_usage(path: Path) -> int:
    """Async wrapper around the recursive `os.scandir` walk.

    `os.scandir` is synchronous and blocking; offloading to a worker thread
    via `asyncio.to_thread` keeps the event loop responsive while still
    reporting exact on-disk consumption.
    """
    return await asyncio.to_thread(_walk_disk_usage, path)


class RepoLockRegistry:
    """Process-wide per-repo `asyncio.Lock` registry.

    Every bare repo gets its own lock on first acquire. The lock is kept
    alive as long as at least one holder is waiting, and removed from the
    registry when the last holder releases — so long-idle repos do not leak
    `asyncio.Lock` objects.

    The intended usage is one acquire per repo write path (`create`, `delete`,
    and the WP-10 git smart-HTTP push handler), so concurrent writes to the
    same repo serialize through `asyncio.Lock` and writes to *different*
    repos proceed in parallel.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._refcounts: dict[tuple[str, str], int] = {}

    @asynccontextmanager
    async def acquire(self, owner: str, name: str) -> AsyncIterator[None]:
        """Hold the per-repo write lock for the duration of the `async with`.

        The body runs once the lock is held; releasing happens automatically
        on exit, including the `KeyboardInterrupt` and `asyncio.CancelledError`
        paths. The lock entry is freed only after the reference count
        drops to zero.
        """
        key = (owner, name)
        # Check-and-create is sync-only (no `await` until the next line),
        # so it is race-free under the asyncio single-threaded scheduler.
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
            self._refcounts[key] = 0
        self._refcounts[key] += 1
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._refcounts[key] -= 1
            if self._refcounts[key] <= 0:
                del self._locks[key]
                del self._refcounts[key]


__all__ = ["REPO_LOCKS", "RepoLockRegistry", "disk_usage", "repo_exists", "repo_fs_path"]


# Process-wide singleton — every write path imports the same registry so a
# create in `create_repo` and a delete in `delete_repo` serialize against
# each other for the same `(owner, name)`.
REPO_LOCKS = RepoLockRegistry()
