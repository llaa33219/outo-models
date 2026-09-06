"""Internal helpers for the `resolve` raw-file endpoint.

The split keeps the public router (`resolve.py`) focused on HTTP-shape
concerns — auth, visibility, response headers — while the git-side
machinery (blob resolution, range parsing, streaming, LFS pointer
sniff) lives here. The split follows the existing `_auth_helpers.py`,
`_admin_helpers.py`, `_ui_helpers.py` pattern in the routers package.

Nothing outside `resolve.py` should import this module.

# allow: SIZE_OK — the helpers below are a single conceptual layer
# (file resolution + Range parsing + LFS sniff + streaming iterator)
# that only `resolve.py` consumes. Splitting into a third file would
# create artificial boundaries without reducing coupling.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass

from dulwich.errors import NotGitRepository
from dulwich.objects import Blob, ObjectID, ShaFile, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo as _DulwichRepo

from outo_models.exceptions import NotFoundError
from outo_models.repos.card import resolve_tip_sha
from outo_models.repos.storage import repo_fs_path

# Streaming chunk size for resolved bodies. Matches
# `objectstore.local.LocalObjectStore._READ_CHUNK` so a typical pull
# does not inflate into thousands of `send` calls.
_RESOLVE_CHUNK = 64 * 1024

# LFS pointer file marker. The spec says:
# `version https://git-lfs.github.com/spec/v1`. We accept the prefix
# verbatim; the rest of the pointer body must include `oid sha256:<hex>`
# and `size N` lines for the redirect to fire.
_LFS_POINTER_PREFIX = b"version https://git-lfs"

# Content-type lookup. `.md` is treated as text/markdown per the HF
# `resolve` endpoint; everything else falls through to
# `application/octet-stream`.
_CONTENT_TYPES: dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
}


def content_type_for(path: str) -> str:
    """Return the response `Content-Type` for `path`; default is octet-stream."""
    _, ext = os.path.splitext(path.lower())
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


@dataclass(frozen=True, slots=True)
class LfsPointer:
    """A parsed git-lfs pointer (`version ...` text body)."""

    oid: str
    size: int


_OID_RE = re.compile(rb"^oid\s+sha256:([0-9a-f]{64})\s*$", re.MULTILINE)
_SIZE_RE = re.compile(rb"^size\s+(\d+)\s*$", re.MULTILINE)


def maybe_lfs_pointer(data: bytes) -> LfsPointer | None:
    """Return a parsed pointer if `data` matches the LFS text format.

    A partial match (one line present, the other missing) is treated as
    ordinary content — a stray `oid sha256:...` in a real file must NOT
    trigger the LFS redirect.
    """
    if not data.startswith(_LFS_POINTER_PREFIX):
        return None
    oid_match = _OID_RE.search(data)
    size_match = _SIZE_RE.search(data)
    if oid_match is None or size_match is None:
        return None
    return LfsPointer(oid=oid_match.group(1).decode("ascii"), size=int(size_match.group(1)))


def resolve_commit_sha(
    repo: _DulwichRepo, revision: str, default_branch: str = "main"
) -> bytes | None:
    """Translate `revision` (branch / tag / sha) to the commit SHA.

    Branch names resolve via `resolve_tip_sha` (the same helper the card
    endpoint uses); tag names are resolved via `refs/tags/<name>`; bare
    SHAs are validated against the object store.

    For branch names we additionally require the resolved SHA to be the
    tip of an actual `refs/heads/<revision>` ref — except when the
    requested revision IS the repo's recorded default branch: a repo
    whose only branch is `master` (older git defaults) must still serve
    `resolve/main/...`, so the default branch name falls back through
    `resolve_tip_sha` (HEAD symref, then the single branch if exactly
    one). Any other non-existent branch 404s. Bare SHA inputs skip the
    branch-resolvability gate so a 40-char commit identifier is
    always validated against the object store directly.
    """
    is_sha = bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", revision))

    branch_ref = Ref(f"refs/heads/{revision}".encode())
    try:
        direct = repo.refs.read_ref(branch_ref)
        if direct:
            return direct
    except (KeyError, ValueError):
        pass

    if not is_sha and (_branch_is_resolvable(repo, revision) or revision == default_branch):
        tip = resolve_tip_sha(repo, revision)
        if tip is not None:
            return tip

    try:
        tag_sha = repo.refs.read_ref(Ref(f"refs/tags/{revision}".encode()))
        if tag_sha is not None:
            obj = repo[tag_sha]
            type_name = getattr(obj, "type_name", None)
            if type_name in (b"commit", "commit"):
                return tag_sha
            obj_sha = getattr(obj, "object", None)
            if isinstance(obj_sha, (bytes, bytearray)) and obj_sha:
                return bytes(obj_sha)
    except (KeyError, ValueError):
        pass

    if is_sha:
        try:
            candidate: ObjectID = ObjectID(bytes.fromhex(revision))
        except ValueError:
            return None
        if candidate in repo.object_store:
            try:
                obj = repo[candidate]
                if getattr(obj, "type_name", None) in (b"commit", "commit"):
                    return candidate
            except (KeyError, NotGitRepository):
                return None

    return None


def _branch_is_resolvable(repo: _DulwichRepo, branch: str) -> bool:
    """Return True if `branch` matches an existing branch ref.

    Used to gate `resolve_tip_sha`'s HEAD fallback so non-existent
    branch names return 404 instead of HEAD.
    """
    # 1. The ref exists verbatim.
    branch_ref = Ref(f"refs/heads/{branch}".encode())
    try:
        if repo.refs.read_ref(branch_ref):
            return True
    except (KeyError, ValueError):
        pass
    # 2. HEAD's symbolic target matches the requested branch.
    try:
        head_raw = repo.refs.read_ref(Ref(b"HEAD"))
    except (KeyError, ValueError):
        return False
    if head_raw and head_raw.startswith(b"ref: refs/heads/"):
        head_branch = head_raw[len(b"ref: refs/heads/") :].strip()
        return head_branch == branch.encode()
    # 3. The repo has exactly one branch and it matches the request.
    branches = [k for k in repo.refs if k.startswith(b"refs/heads/")]
    if len(branches) == 1:
        only = branches[0][len(b"refs/heads/") :]
        return only == branch.encode()
    return False


def _walk_tree(repo: _DulwichRepo, tree: Tree, segments: list[str]) -> ObjectID | None:
    """Descend `segments` through `tree`; return the leaf blob SHA.

    A non-directory intermediate is treated as "path not found" so the
    caller returns 404. Symlinks and submodules are rejected with the
    same response — only plain blobs are served.
    """
    store = repo.object_store

    def _lookup(sha: ObjectID) -> ShaFile:
        return store[sha]

    current: Tree | ShaFile = tree
    for idx, segment in enumerate(segments):
        if not isinstance(current, Tree):
            return None
        try:
            _mode, sha = current.lookup_path(_lookup, segment.encode("utf-8"))
        except (KeyError, NotGitRepository):
            return None
        try:
            obj = _lookup(sha)
        except (KeyError, NotGitRepository):
            return None
        is_last = idx == len(segments) - 1
        if is_last:
            if not isinstance(obj, Blob):
                return None
            return sha
        if not isinstance(obj, Tree):
            return None
        current = obj

    return None


def resolve_blob_sync(
    owner: str, name: str, revision: str, path: str, default_branch: str = "main"
) -> tuple[ObjectID, int]:
    """Resolve `(owner, name, revision, path)` to `(blob_sha, size)`.

    Raises `NotFoundError` (mapped to 404) for any missing piece. The
    blob object itself is NOT loaded here so the streaming handler can
    hand the sha back to dulwich without re-walking the tree.
    """
    fs_path = repo_fs_path(owner, name)
    if not fs_path.exists():
        raise NotFoundError(f"file not found: {owner}/{name}/resolve/{revision}/{path}")

    repo = _DulwichRepo(str(fs_path))
    try:
        commit_sha = resolve_commit_sha(repo, revision, default_branch)
        if commit_sha is None:
            raise NotFoundError(f"revision not found: {revision}")
        try:
            commit = repo[commit_sha]
        except (KeyError, NotGitRepository) as exc:
            raise NotFoundError(f"revision not found: {revision}") from exc
        tree_sha = getattr(commit, "tree", None)
        missing = f"file not found: {owner}/{name}/resolve/{revision}/{path}"
        if not isinstance(tree_sha, bytes) or not tree_sha:
            raise NotFoundError(missing)
        try:
            root = repo[tree_sha]
        except (KeyError, NotGitRepository) as exc:
            raise NotFoundError(missing) from exc
        if not isinstance(root, Tree):
            raise NotFoundError(missing)

        cleaned = path.replace("\\", "").strip("/")
        if cleaned in ("", "."):
            raise NotFoundError(missing)
        parts = cleaned.split("/")
        for part in parts:
            if not part or part in (".", ".."):
                raise NotFoundError(missing)

        blob_sha = _walk_tree(repo, root, parts)
        if blob_sha is None:
            raise NotFoundError(missing)
        try:
            size = repo.object_store[blob_sha].raw_length()
        except (KeyError, NotGitRepository) as exc:
            raise NotFoundError(missing) from exc
        return blob_sha, int(size)
    finally:
        repo.close()


async def resolve_blob(
    owner: str, name: str, revision: str, path: str, default_branch: str = "main"
) -> tuple[ObjectID, int]:
    """Async wrapper around `resolve_blob_sync`."""
    return await asyncio.to_thread(resolve_blob_sync, owner, name, revision, path, default_branch)


def iter_blob_window(
    repo_path: str, blob_sha: ObjectID, *, start: int, end: int
) -> Iterator[bytes]:
    """Yield bytes from `blob_sha[start:end+1]` in `_RESOLVE_CHUNK` slices.

    Streams the blob lazily so peak memory is O(chunk) — verified for a
    500 MiB blob. The function is a synchronous iterator so FastAPI's
    `StreamingResponse` can drive it from a worker thread.

    `start` and `end` are inclusive byte offsets; `end == -1` means "to
    the end of the blob".
    """
    repo = _DulwichRepo(repo_path)
    try:
        store = repo.object_store
        try:
            blob_obj = store[blob_sha]
        except (KeyError, NotGitRepository):
            return
        if not isinstance(blob_obj, Blob):
            return
        consumed = 0
        buf = bytearray()
        out_limit = 0 if end == -1 else (end - start + 1)
        emitted = 0
        for chunk in blob_obj.as_raw_chunks():
            if not chunk:
                continue
            chunk_len = len(chunk)
            chunk_end = consumed + chunk_len
            if chunk_end <= start:
                consumed = chunk_end
                continue
            local_start = max(0, start - consumed)
            buf.extend(chunk[local_start:])
            consumed = chunk_end
            while len(buf) >= _RESOLVE_CHUNK:
                piece = bytes(buf[:_RESOLVE_CHUNK])
                del buf[:_RESOLVE_CHUNK]
                if out_limit and emitted + len(piece) > out_limit:
                    piece = piece[: out_limit - emitted]
                emitted += len(piece)
                yield piece
                if out_limit and emitted >= out_limit:
                    return
            if end != -1 and consumed > end:
                break
        if buf:
            piece = bytes(buf)
            if out_limit and emitted + len(piece) > out_limit:
                piece = piece[: out_limit - emitted]
            if piece:
                yield piece
    finally:
        repo.close()


def peek_blob_head(repo_path: str, blob_sha: ObjectID, *, max_bytes: int) -> bytes | None:
    """Return the first `max_bytes` of a blob, or `None` on read failure.

    Used to sniff for the LFS pointer marker without materialising the
    full blob.
    """
    repo = _DulwichRepo(repo_path)
    try:
        store = repo.object_store
        try:
            obj = store[blob_sha]
        except (KeyError, NotGitRepository):
            return None
        if not isinstance(obj, Blob):
            return None
        chunks = obj.as_raw_chunks()
        head = bytearray()
        for chunk in chunks:
            if not chunk:
                continue
            head.extend(chunk)
            if len(head) >= max_bytes:
                break
        return bytes(head[:max_bytes])
    finally:
        repo.close()


@dataclass(frozen=True, slots=True)
class HttpRange:
    """A successful HTTP Range parse."""

    start: int
    end: int

    @property
    def satisfiable(self) -> bool:
        """`True` if the window fits within a non-empty resource."""
        return self.start >= 0 and self.end >= self.start


def parse_range(header: str | None, total_size: int) -> HttpRange | None:
    """Parse a `Range: bytes=start-end` header.

    Returns `None` for missing headers (full body), an `HttpRange` with
    `satisfiable=False` for parse errors AND out-of-range windows, and a
    normal `HttpRange` otherwise. We do NOT raise on bad input — the
    caller maps the sentinel to 416.
    """
    if not header:
        return None
    if not header.lower().startswith("bytes="):
        return HttpRange(start=-1, end=-1)
    spec = header[len("bytes=") :].strip()
    if not spec or "," in spec:
        return HttpRange(start=-1, end=-1)
    if "-" not in spec:
        return HttpRange(start=-1, end=-1)
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "" and end_s == "":
            return HttpRange(start=-1, end=-1)
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0 or total_size <= 0:
                return HttpRange(start=-1, end=-1)
            start = max(0, total_size - suffix)
            end = total_size - 1
        elif end_s == "":
            start = int(start_s)
            end = total_size - 1
        else:
            start = int(start_s)
            end = int(end_s)
    except ValueError:
        return HttpRange(start=-1, end=-1)
    if start < 0 or end < start or (total_size > 0 and start >= total_size):
        return HttpRange(start=-1, end=-1)
    if end >= total_size:
        end = total_size - 1
    return HttpRange(start=start, end=end)


__all__ = [
    "HttpRange",
    "LfsPointer",
    "content_type_for",
    "iter_blob_window",
    "maybe_lfs_pointer",
    "parse_range",
    "peek_blob_head",
    "resolve_blob",
    "resolve_commit_sha",
]
