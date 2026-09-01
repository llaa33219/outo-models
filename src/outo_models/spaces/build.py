"""Build contexts for Space containers.

A "build context" is a tarball of the Space repo's *working tree* at its
current default branch — exactly what `docker build` / `podman build` consume
in their simplest form. The tar is produced *in memory* from dulwich object
reads: no shell-out to `git`, no temporary directory on disk, no `git clone`
or `git archive` subprocess. `make_build_context` is the single public
surface the runtime manager depends on.

The tree walk is intentionally recursive: nested directories are descended
in their natural order, and each blob becomes one tar entry at the
appropriate relative path. No `.git/` is ever included because the walk
starts from the commit's tree, not from a working-directory scan.

`export_static_site` is the specialisation the `static` SDK uses: it dumps
the same tree, but onto the local filesystem (`<spaces_dir>/<owner>/<name>/site/`),
so the proxy route can serve it without a container. The runtime manager
calls `export_static_site` from `start()` when the space SDK is `static`.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dulwich import objects as dulwich_objects
from dulwich.repo import Repo as DulwichRepo

from outo_models.repos.storage import repo_fs_path
from outo_models.utils.paths import spaces_dir

_BUILD_EXCLUDED_DIRS: frozenset[str] = frozenset({".git", ".hg", "__pycache__"})


def _iter_tree_blobs(store: Any, tree_sha: bytes | None) -> Iterator[tuple[str, int, bytes]]:
    """Yield `(relpath, mode, data)` for every regular file in `tree_sha`."""
    if tree_sha is None:
        return
    stack: list[tuple[bytes, str]] = [(tree_sha, "")]
    while stack:
        current_sha, prefix = stack.pop()
        tree_obj = store[current_sha]
        for entry in dulwich_objects.Tree.iteritems(tree_obj):
            raw_name = entry.path
            name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
            mode = entry.mode
            if prefix and any(part in _BUILD_EXCLUDED_DIRS for part in prefix.split("/")):
                continue
            relpath = f"{prefix}{name}"
            if mode == 0o040000:
                if name in _BUILD_EXCLUDED_DIRS:
                    continue
                stack.append((entry.sha, f"{relpath}/"))
                continue
            if mode == 0o160000 or mode == 0o040000 | 0o200000:
                continue
            blob = store[entry.sha]
            yield relpath, mode, blob.as_raw_string()


def _make_tar_bytes(store: Any, tree_sha: bytes | None) -> bytes:
    """Render the tree rooted at `tree_sha` into a gzipped tarball blob.

    `tree_sha=None` produces a valid empty tar so callers can still POST a
    build context — podman accepts a zero-file context and the resulting
    image contains whatever the base image provides.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if tree_sha is None:
            return buf.getvalue()
        for relpath, mode, data in _iter_tree_blobs(store, tree_sha):
            info = tarfile.TarInfo(name=relpath)
            info.size = len(data)
            info.mode = mode & 0o7777
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _resolve_tree_sha(repo: DulwichRepo) -> bytes | None:
    """Return the SHA of the HEAD commit's tree, or `None` for empty repos.

    Freshly-initialised bare repos have neither a HEAD commit nor a
    resolved ref. Returning `None` lets callers proceed gracefully
    instead of crashing the runtime on the very first push.
    """
    try:
        head_sha = repo.head()
    except (KeyError, ValueError):
        return None
    if head_sha is None:
        return None
    commit = repo[head_sha]
    tree_sha = commit.tree  # type: ignore[attr-defined]
    return tree_sha if isinstance(tree_sha, bytes) and tree_sha else None


def make_build_context(owner: str, name: str) -> bytes:
    """Export the default-branch tree of `<owner>/<name>` as a build-context tar."""
    bare_path = repo_fs_path(owner, name)
    repo = DulwichRepo(str(bare_path))
    try:
        tree_sha = _resolve_tree_sha(repo)
        return _make_tar_bytes(repo.object_store, tree_sha)
    finally:
        repo.close()


def export_static_site(owner: str, name: str, dest: Path) -> None:
    """Materialize the Space tree into `dest` for the static-proxy path."""
    bare_path = repo_fs_path(owner, name)
    repo = DulwichRepo(str(bare_path))
    try:
        tree_sha = _resolve_tree_sha(repo)
        if tree_sha is None:
            dest.mkdir(parents=True, exist_ok=True)
            return
        store = repo.object_store
        for relpath, _mode, data in _iter_tree_blobs(store, tree_sha):
            target = dest / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    finally:
        repo.close()


def static_site_dir(owner: str, name: str) -> Path:
    """Return the disk path where `export_static_site` writes a `static` space."""
    return spaces_dir() / owner / name / "site"


__all__ = ["export_static_site", "make_build_context", "static_site_dir"]
