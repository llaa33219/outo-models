"""Unit tests for `outo_models.repos.storage`.

The on-disk primitives are exercised against a real temporary filesystem
(no mocking) — they are the boundary every other repos function sits on,
so they need to behave correctly under `tmp_data_dir`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from outo_models.repos.storage import (
    RepoLockRegistry,
    disk_usage,
    repo_exists,
    repo_fs_path,
)


class TestRepoFsPath:
    """`repo_fs_path` mirrors the layout documented in `utils.paths`."""

    def test_path_is_under_repos_dir(self, tmp_data_dir: Path) -> None:
        path = repo_fs_path("alice", "model-a")
        assert path.parent.parent == tmp_data_dir / "repos"
        assert path.name == "model-a.git"
        assert path.parent.name == "alice"


class TestRepoExists:
    """`repo_exists` reflects the on-disk presence of the bare repo dir."""

    def test_returns_false_when_missing(self, tmp_data_dir: Path) -> None:
        assert repo_exists("alice", "model-a") is False

    def test_returns_true_when_present(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = repo_fs_path("alice", "model-a")
        path.mkdir(parents=True)
        assert repo_exists("alice", "model-a") is True


class TestDiskUsage:
    """`disk_usage` walks the tree without following symlinks."""

    async def test_returns_zero_for_missing_path(self, tmp_data_dir: Path) -> None:
        assert await disk_usage(tmp_data_dir / "nope") == 0

    async def test_returns_zero_for_empty_dir(self, tmp_data_dir: Path) -> None:
        empty = tmp_data_dir / "empty"
        empty.mkdir()
        assert await disk_usage(empty) == 0

    async def test_sums_file_sizes(self, tmp_data_dir: Path) -> None:
        root = tmp_data_dir / "tree"
        root.mkdir()
        (root / "a").write_bytes(b"a" * 100)
        nested = root / "nested"
        nested.mkdir()
        (nested / "b").write_bytes(b"b" * 200)
        assert await disk_usage(root) == 300

    async def test_does_not_follow_symlinks(self, tmp_data_dir: Path) -> None:
        # Outside file the symlink points at; must NOT be counted.
        outside = tmp_data_dir / "outside.bin"
        outside.write_bytes(b"x" * 10_000)

        root = tmp_data_dir / "tree"
        root.mkdir()
        (root / "real").write_bytes(b"y" * 100)
        # Symlink to outside; following would inflate the count by ~9900.
        (root / "link").symlink_to(outside)

        assert await disk_usage(root) == 100

    async def test_top_level_symlink_returns_zero(self, tmp_data_dir: Path) -> None:
        target = tmp_data_dir / "real"
        target.mkdir()
        (target / "f").write_bytes(b"z" * 4096)
        link = tmp_data_dir / "link"
        link.symlink_to(target)
        # The top-level path is itself a symlink — refuse to walk it.
        assert await disk_usage(link) == 0


class TestRepoLockRegistry:
    """`RepoLockRegistry` serializes per-repo writes and reclaims idle locks."""

    async def test_acquire_is_async_context_manager(self) -> None:
        registry = RepoLockRegistry()
        async with registry.acquire("alice", "model-a"):
            assert ("alice", "model-a") in registry._locks

    async def test_concurrent_same_repo_serializes(self) -> None:
        registry = RepoLockRegistry()
        order: list[str] = []

        async def slow() -> None:
            async with registry.acquire("alice", "model-a"):
                order.append("slow-enter")
                await asyncio.sleep(0.05)
                order.append("slow-exit")

        async def fast() -> None:
            async with registry.acquire("alice", "model-a"):
                order.append("fast-enter")
                order.append("fast-exit")

        await asyncio.gather(slow(), fast())
        # `slow` must complete before `fast` enters the critical section.
        assert order == ["slow-enter", "slow-exit", "fast-enter", "fast-exit"]

    async def test_concurrent_different_repos_do_not_serialize(self) -> None:
        registry = RepoLockRegistry()
        order: list[str] = []
        half = 0.05

        async def worker(name: str) -> None:
            async with registry.acquire("alice", name):
                order.append(f"{name}-enter")
                await asyncio.sleep(half)
                order.append(f"{name}-exit")

        start = time.monotonic()
        await asyncio.gather(worker("a"), worker("b"))
        elapsed = time.monotonic() - start
        # If the two workers serialized, total time would be ~2*half.
        assert elapsed < half * 1.8

    async def test_lock_reclaimed_after_last_release(self) -> None:
        registry = RepoLockRegistry()

        async def hold() -> None:
            async with registry.acquire("alice", "model-a"):
                pass

        await hold()
        # Last holder released — registry should drop the entry so it does
        # not leak over a long-running process.
        assert ("alice", "model-a") not in registry._locks

    async def test_overlapping_holders_keep_lock_alive(self) -> None:
        registry = RepoLockRegistry()
        entered = asyncio.Event()
        release_outer = asyncio.Event()

        async def outer() -> None:
            async with registry.acquire("alice", "model-a"):
                entered.set()
                await release_outer.wait()

        task = asyncio.create_task(outer())
        await entered.wait()

        # The outer coroutine is now holding the lock. Spawn a second
        # acquire that serializes behind it — it must NOT enter the
        # critical section until `release_outer` is set.
        inner_done = asyncio.Event()

        async def inner() -> None:
            async with registry.acquire("alice", "model-a"):
                inner_done.set()

        inner_task = asyncio.create_task(inner())
        # Yield to give `inner_task` a chance to attempt the acquire; it
        # must be blocked because the outer holder has not released yet.
        await asyncio.sleep(0)
        assert not inner_task.done()

        release_outer.set()
        await task
        await inner_task
        assert inner_done.is_set()

        # Both holders released — registry reclaims the entry.
        assert ("alice", "model-a") not in registry._locks
