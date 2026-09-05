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
from dulwich.refs import Ref
from dulwich.repo import Repo as _DulwichRepo

from outo_models.exceptions import NotFoundError
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
    try:
        head_sha = repo.refs.read_ref(Ref(f"refs/heads/{branch}".encode()))
    except (KeyError, ValueError):
        return None
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
            rows.append(
                FileEntry(
                    name=name,
                    path=name,
                    kind="file",
                    size_bytes=len(obj.data),
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
