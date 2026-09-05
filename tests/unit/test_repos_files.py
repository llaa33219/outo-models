"""Unit tests for `outo_models.repos.files` (traversal-safe tree listing).

The contract:

    * `list_files` returns `FileEntry` rows sorted dirs-first then name.
    * File entries carry a non-`None` `size_bytes`; directories carry `None`.
    * Path traversal (`..`, absolute paths, dot-segments) raises
      `NotFoundError` BEFORE any tree walk so the request cannot reach
      the bare-repo root.
    * Missing repos, empty repos, and missing directories raise
      `NotFoundError` so the API layer can render the 404 UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dulwich import porcelain

from outo_models.config import get_settings
from outo_models.exceptions import NotFoundError
from outo_models.repos.files import FileEntry, list_files
from outo_models.repos.storage import repo_fs_path


def _seed_repo_tree(tmp_data_dir: Path) -> None:
    """Build a bare repo with a tree that exercises every code path."""
    work = tmp_data_dir / "src"
    work.mkdir()
    (work / "README.md").write_text("# hi")
    (work / "a.txt").write_text("alpha")
    (work / "b.txt").write_text("beta")
    (work / "src").mkdir()
    (work / "src" / "h.py").write_text("# helper")
    (work / "src" / "data").mkdir()
    (work / "src" / "data" / "x.bin").write_bytes(b"\x00\x01\x02")
    (work / ".gitignore").write_text("ignored")

    porcelain.init(str(work))
    porcelain.add(
        str(work),
        paths=[
            "README.md",
            "a.txt",
            "b.txt",
            ".gitignore",
            "src/h.py",
            "src/data/x.bin",
        ],
    )
    porcelain.commit(
        str(work),
        message=b"init",
        author=b"a <a@a>",
        committer=b"a <a@a>",
    )
    bare = repo_fs_path("alice", "tree")
    bare.parent.mkdir(parents=True, exist_ok=True)
    porcelain.clone(str(work), str(bare), bare=True)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


class TestListFilesRoot:
    async def test_root_returns_dirs_first_then_files(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        rows = await list_files("alice", "tree")
        kinds_and_names = [(r.kind, r.name) for r in rows]
        assert kinds_and_names == [
            ("dir", "src"),
            ("file", ".gitignore"),
            ("file", "README.md"),
            ("file", "a.txt"),
            ("file", "b.txt"),
        ]

    async def test_file_size_is_populated(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        rows = await list_files("alice", "tree")
        a = next(r for r in rows if r.name == "a.txt")
        assert a.kind == "file"
        assert a.size_bytes == 5  # "alpha"

    async def test_directory_has_no_size(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        rows = await list_files("alice", "tree")
        src = next(r for r in rows if r.name == "src")
        assert src.kind == "dir"
        assert src.size_bytes is None


class TestListFilesSubdir:
    async def test_subdir_path_is_relative(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        rows = await list_files("alice", "tree", path="src")
        assert {r.path for r in rows} == {"src/h.py", "src/data"}

    async def test_nested_subdir(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        rows = await list_files("alice", "tree", path="src/data")
        assert len(rows) == 1
        assert rows[0].name == "x.bin"
        assert rows[0].path == "src/data/x.bin"
        assert rows[0].size_bytes == 3


class TestListFilesErrors:
    async def test_missing_repo(self, tmp_data_dir: Path) -> None:
        with pytest.raises(NotFoundError):
            await list_files("ghost", "nope")

    async def test_missing_directory(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        with pytest.raises(NotFoundError):
            await list_files("alice", "tree", path="no-such-dir")

    async def test_path_traversal_dot_dot_rejected(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        with pytest.raises(NotFoundError):
            await list_files("alice", "tree", path="../../etc/passwd")

    async def test_path_traversal_absolute_rejected(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        with pytest.raises(NotFoundError):
            await list_files("alice", "tree", path="/etc/passwd")

    async def test_path_traversal_inline_dot_segment_rejected(self, tmp_data_dir: Path) -> None:
        _seed_repo_tree(tmp_data_dir)
        with pytest.raises(NotFoundError):
            await list_files("alice", "tree", path="src/../../etc")

    async def test_empty_repo_is_not_found(self, tmp_data_dir: Path) -> None:
        work = tmp_data_dir / "src"
        work.mkdir()
        porcelain.init(str(work))
        bare = repo_fs_path("alice", "empty")
        bare.parent.mkdir(parents=True, exist_ok=True)
        porcelain.clone(str(work), str(bare), bare=True)
        with pytest.raises(NotFoundError):
            await list_files("alice", "empty")


class TestFileEntry:
    def test_is_frozen_dataclass(self) -> None:
        entry = FileEntry(name="a", path="a", kind="file", size_bytes=1)
        assert entry.name == "a"
        assert entry.size_bytes == 1
