"""File-tree listing for the repo-detail UI.

`list_files` reads the bare repo's tree via dulwich (no worktree
checkout, no `git ls-tree` subprocess) and returns a flat, sorted
listing suitable for rendering one folder at a time. Path traversal
attempts (`..`, absolute paths) are rejected before any tree walk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from dulwich.errors import NotGitRepository
from dulwich.objects import Blob, ObjectID, ShaFile, Tree
from dulwich.repo import Repo as _DulwichRepo

from outo_models.exceptions import NotFoundError
from outo_models.repos.card import resolve_tip_sha
from outo_models.repos.storage import repo_fs_path

_DIR_MODE = 0o040000
_SYMLINK_MODE = 0o120000
_SUBMODULE_MODE = 0o160000


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One row in a repo file listing.

    `size_bytes` is `None` for `kind == "dir"` — directories in git have
    no inherent size; the value is intentionally nullable so the JSON
    shape stays predictable across rows.
    """

    name: str
    path: str
    kind: str  # "file" | "dir"
    size_bytes: int | None
    # True when the blob is a Git LFS pointer — `size_bytes` then holds the
    # POINTER'S declared size (the real object size), not the pointer text
    # length. Field failure: every LFS-backed file listed as ~135 B.
    lfs: bool = False


def _validate_path(path: str) -> list[str]:
    """Return the normalised path segments or raise `NotFoundError`.

    Rejects empty segments, `.`, `..`, and absolute paths before the
    tree walk so a malicious `?path=../../etc` query cannot escape the
    repo root. Returns the list of segment names (no leading `/`).
    """
    if path.startswith("/"):
        raise NotFoundError(f"path not found: {path!r}")
    cleaned = path.replace(
        "\\",
        "",
    ).strip("/")
    if cleaned in ("", "."):
        return []
    parts = cleaned.split("/")
    for part in parts:
        if not part or part in (".", ".."):
            raise NotFoundError(f"path not found: {path!r}")
    return parts


def _default_branch_tree(repo: _DulwichRepo, branch: str) -> Tree | None:
    """Resolve `branch`'s tip tree (same helper semantics as `card.py`)."""
    head_sha = resolve_tip_sha(repo, branch)
    if head_sha is None:
        return None
    try:
        commit = repo[head_sha]
    except (KeyError, NotGitRepository):
        return None
    tree_sha = getattr(commit, "tree", None)
    if not isinstance(tree_sha, bytes) or not tree_sha:
        return None
    try:
        root: ShaFile = repo[tree_sha]
    except (KeyError, NotGitRepository):
        return None
    return root if isinstance(root, Tree) else None


def _descend(tree: Tree, segments: list[str], repo: _DulwichRepo) -> Tree:
    """Walk into `segments` of `tree`; raise `NotFoundError` on miss."""
    store = repo.object_store

    def _lookup(sha: ObjectID) -> ShaFile:
        return store[sha]

    current: Tree = tree
    for segment in segments:
        try:
            mode, sha = current.lookup_path(_lookup, segment.encode())
        except (KeyError, NotGitRepository) as exc:
            raise NotFoundError(f"path not found: {'/'.join(segments)!r}") from exc
        if mode != _DIR_MODE:
            raise NotFoundError(f"path not found: {'/'.join(segments)!r}")
        try:
            next_obj: ShaFile = store[sha]
        except (KeyError, NotGitRepository) as exc:
            raise NotFoundError(f"path not found: {'/'.join(segments)!r}") from exc
        if not isinstance(next_obj, Tree):
            raise NotFoundError(f"path not found: {'/'.join(segments)!r}")
        current = next_obj
    return current


_LFS_PREFIX = b"version https://git-lfs"


def _lfs_pointer_size(data: bytes) -> int | None:
    """Return the declared size of an LFS pointer blob, or `None`.

    Pointer text is three lines (`version` / `oid sha256:…` / `size N`);
    anything bigger than 4 KiB or not starting with the version line is
    treated as a normal blob without a second read.
    """
    if len(data) > 4096 or not data.startswith(_LFS_PREFIX):
        return None
    has_oid = False
    size: int | None = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("oid sha256:"):
            has_oid = True
        elif line.startswith("size "):
            try:
                size = int(line[len("size ") :].strip())
            except ValueError:
                return None
    return size if has_oid and size is not None else None


def _list_one_level(tree: Tree, lookup: Callable[[ObjectID], ShaFile]) -> list[FileEntry]:
    """Return direct children of `tree` sorted dirs-first then by name."""
    rows: list[FileEntry] = []
    for entry in tree.items():
        raw_name = entry.path
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
        if not name:
            continue
        mode = int(entry.mode)
        sha = entry.sha
        if mode == _DIR_MODE:
            rows.append(FileEntry(name=name, path=name, kind="dir", size_bytes=None))
            continue
        if mode in (_SYMLINK_MODE, _SUBMODULE_MODE):
            continue
        try:
            obj = lookup(sha)
        except (KeyError, NotGitRepository):
            continue
        if isinstance(obj, Blob):
            size: int = len(obj.data)
            is_lfs = False
            if size < 4096:
                pointer_size = _lfs_pointer_size(obj.data)
                if pointer_size is not None:
                    size = pointer_size
                    is_lfs = True
            rows.append(
                FileEntry(
                    name=name,
                    path=name,
                    kind="file",
                    size_bytes=size,
                    lfs=is_lfs,
                )
            )
    rows.sort(key=lambda r: (0 if r.kind == "dir" else 1, r.name))
    return rows


def _list_files_sync(owner: str, name: str, *, default_branch: str, path: str) -> list[FileEntry]:
    """Sync `list_files`; runs in a worker thread."""
    fs_path = repo_fs_path(owner, name)
    if not fs_path.exists():
        raise NotFoundError(f"repository not found: {owner}/{name}")
    segments = _validate_path(path)
    repo = _DulwichRepo(str(fs_path))
    try:
        tree = _default_branch_tree(repo, default_branch)
        if tree is None:
            raise NotFoundError(f"repository not found: {owner}/{name}")

        def _lookup(sha: ObjectID) -> ShaFile:
            return repo.object_store[sha]

        target = _descend(tree, segments, repo)
        rows = _list_one_level(target, _lookup)
        if segments:
            rows = [
                FileEntry(
                    name=row.name,
                    path=f"{'/'.join(segments)}/{row.name}",
                    kind=row.kind,
                    size_bytes=row.size_bytes,
                    lfs=row.lfs,
                )
                for row in rows
            ]
        return rows
    finally:
        repo.close()


async def list_files(
    owner: str,
    name: str,
    path: str = "",
    *,
    ref: str | None = None,
    default_branch: str = "main",
) -> list[FileEntry]:
    """List one directory of `<owner>/<name>` at the resolved branch tip.

    `path` is validated against traversal before any tree access; `..`,
    absolute paths, and `.` segments are rejected with `NotFoundError`.
    `ref` is accepted for forward compatibility but the resolved branch
    is the on-disk default branch for now (sub-directories of a non-tip
    ref are a v2 feature).

    Raises `NotFoundError` for missing repos, empty repos, and missing
    directories so callers can render the 404 UI without branching.
    """
    del ref  # see docstring; v2 will resolve arbitrary refs
    return await asyncio.to_thread(
        _list_files_sync, owner, name, default_branch=default_branch, path=path
    )


__all__ = ["FileEntry", "list_files"]
