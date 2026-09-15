"""Unit tests for `outo_models.repos.commit.commit_files`.

Covers the v0.5.x file-commit seam: the helper lands one or more files
as a single commit on the default branch, optionally carrying deletions
(rename = delete-old + add-new) so the editor's rename action can reuse
the same atomic-commit path.

The existing tests around upload + edit live in
`tests/integration/test_ui_repo_page.py`; this file pins the helper's
contract directly so the rename feature can rely on the deletion seam
without going through the HTTP layer.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.repo import Repo as _DulwichRepo

from outo_models.config import get_settings
from outo_models.repos.commit import commit_files
from outo_models.repos.storage import repo_fs_path


def _seed_bare_repo(tmp_data_dir: Path, owner: str, name: str, files: dict[str, bytes]) -> None:
    """Build a bare repo seeded with the given bytes, default branch `main`."""
    work = tmp_data_dir / "src-repo"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    for rel_path, content in files.items():
        target = work / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    porcelain.init(str(work))
    porcelain.add(str(work), paths=list(files.keys()))
    porcelain.commit(
        str(work),
        message=b"init",
        author=b"alice <a@example.com>",
        committer=b"alice <a@example.com>",
    )
    bare = repo_fs_path(owner, name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    if bare.exists():
        shutil.rmtree(bare)
    porcelain.clone(str(work), str(bare), bare=True)


def _tree_paths(bare_path: Path) -> list[str]:
    """Return the file paths inside the default branch's tree."""
    repo = _DulwichRepo(str(bare_path))
    try:
        commit = repo[b"HEAD"]
        tree = repo[commit.tree]
        return sorted(entry.path.decode() for entry in tree.items())
    finally:
        repo.close()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


class TestCommitFilesDeletions:
    """`commit_files` accepts an optional `deletions` argument."""

    async def test_deletions_remove_files_from_tree(self, tmp_data_dir: Path) -> None:
        _seed_bare_repo(
            tmp_data_dir,
            "alice",
            "del",
            {"README.md": b"# x", "config.json": b"old", "keep.txt": b"keep"},
        )
        result = await commit_files(
            owner="alice",
            name="del",
            default_branch="main",
            actor_username="alice",
            actor_email="a@example.com",
            files={"config.json": b"new"},
            prefix="",
            message="delete+update",
            deletions=["keep.txt"],
        )
        assert result.commit_sha
        paths = _tree_paths(repo_fs_path("alice", "del"))
        assert "config.json" in paths
        assert "keep.txt" not in paths
        assert "README.md" in paths

    async def test_deletions_empty_iterable_is_noop(self, tmp_data_dir: Path) -> None:
        """Existing callers that do NOT pass `deletions` must work unchanged.

        Verified by omitting the keyword: the result includes the written
        path, and no extra files appear in the tree.
        """
        _seed_bare_repo(
            tmp_data_dir,
            "alice",
            "noop",
            {"README.md": b"# x"},
        )
        result = await commit_files(
            owner="alice",
            name="noop",
            default_branch="main",
            actor_username="alice",
            actor_email="a@example.com",
            files={"new.txt": b"hello"},
            prefix="",
            message="add new",
        )
        assert result.commit_sha
        assert "new.txt" in result.paths
        paths = _tree_paths(repo_fs_path("alice", "noop"))
        assert paths == ["README.md", "new.txt"]

    async def test_deletions_rename_one_commit(self, tmp_data_dir: Path) -> None:
        """A rename = delete-old + add-new lands as ONE commit (one SHA)."""
        _seed_bare_repo(
            tmp_data_dir,
            "alice",
            "rename",
            {"README.md": b"# x", "old.txt": b"same content"},
        )
        result = await commit_files(
            owner="alice",
            name="rename",
            default_branch="main",
            actor_username="alice",
            actor_email="a@example.com",
            files={"new.txt": b"same content"},
            prefix="",
            message="rename old to new",
            deletions=["old.txt"],
        )
        assert result.commit_sha
        paths = _tree_paths(repo_fs_path("alice", "rename"))
        assert "old.txt" not in paths
        assert "new.txt" in paths
        # Bytes written is the new content only, not the old + new.
        assert result.bytes_written == len(b"same content")
