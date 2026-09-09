"""Git LFS upload protocol — partition, batch, stream-PUT, pointer text.

The CLI drives the four LFS endpoints the server implements:

    POST  /{owner}/{name}.git/info/lfs/objects/batch
    PUT   /{owner}/{name}.git/info/lfs/objects/{oid}
    GET   /{owner}/{name}.git/info/lfs/objects/{oid}
    GET   /{owner}/{name}/resolve/{revision}/{path:path}    # redirects to the above for LFS blobs

The upload path runs at the boundary between two regimes:

    * Files at or below the per-file multipart cap (100 MiB) ride the
      existing `POST /api/repos/.../upload` endpoint unchanged.
    * Files above the cap use the LFS batch + PUT dance, then are
      committed to the repo as LFS pointer text. The commit happens via
      the same multipart endpoint — the pointer bytes are tiny so they
      fit the multipart limit with no special-casing.

The download path is the mirror image: `GET /resolve/...` returns a 302
to `/info/lfs/objects/{oid}` whenever the on-tree blob is an LFS
pointer, and the CLI's Basic-auth client (see `http.build_basic_async_client`)
follows that redirect transparently because httpx forwards the
`Authorization` header on same-origin redirects.

The wire format is the canonical git-lfs batch shape:

    Request:  {"operation": "upload", "objects": [{"oid", "size"}, ...]}
    Response: {"objects": [{"oid", "size", "actions": {"upload": {"href", "header", "expires_in"}}}
                            | "error": {"code", "message"}]
               | (no `actions` when the object is already on the server)}

Auth is HTTP Basic (`username:PAT`) — Bearer is not accepted on the LFS
surface, so this module assumes the client was built with
`build_basic_client` / `build_basic_async_client`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LFS_CONTENT_TYPE = "application/vnd.git-lfs+json"

#: Chunk size used when streaming a file into the LFS PUT body. Big
#: enough to keep the per-chunk syscalls off the hot path; small enough
#: to bound peak memory at O(chunks-in-flight) regardless of file size.
PUT_CHUNK_BYTES = 1024 * 1024

#: Same chunk size used when computing the sha256 alongside the PUT
#: stream. Identical to `PUT_CHUNK_BYTES` so a single read loop drives both.
HASH_CHUNK_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Pointer text
# ---------------------------------------------------------------------------


def pointer_text(oid: str, size: int) -> bytes:
    """Render the canonical git-lfs pointer text for `(oid, size)`.

    The server's resolve endpoint sniffs the leading `version https://git-lfs`
    line and redirects to the object URL; both the line order and the
    trailing newline are part of the wire contract and MUST match.
    """
    return (f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {size}\n").encode()


# ---------------------------------------------------------------------------
# File partitioning + streaming sha256
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LargeFile:
    """A file that exceeds the multipart cap and must travel through LFS.

    `oid` is computed by streaming the file once at partition time. The
    CLI never loads the bytes into memory; both the digest and the
    subsequent PUT body come from the same on-disk file via independent
    read passes.
    """

    path: Path
    oid: str
    size: int


@dataclass(frozen=True, slots=True)
class Partition:
    """Result of splitting a user-supplied file set into small / large buckets.

    `small` keeps the original `Path` order so the on-disk filename
    ordering round-trips into the multipart `files[]` list. `large` is
    ordered by descending size so the biggest object (whose PUT takes
    the longest) gets a tighter Rich progress ETA — UX nicety, not a
    protocol requirement.
    """

    small: list[Path]
    large: list[LargeFile] = field(default_factory=list)


def _sha256_of(path: Path) -> tuple[str, int]:
    """Stream `path` through sha256; return `(hex_digest, size)`.

    A 1 MiB read buffer keeps memory bounded regardless of file size.
    Errors from the underlying `open()` (file removed between the
    partition step and the PUT) surface unchanged so the caller's
    `OmcError` mapping sees the real cause.
    """
    hasher = hashlib.sha256()
    total = 0
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total


def partition_files(files: Iterable[Path], *, cap_bytes: int) -> Partition:
    """Split `files` into small (multipart) and large (LFS) buckets.

    The function streams each large file exactly once to compute its
    sha256 — the same digest the server's LFS PUT handler will verify
    at completion. Identical `(oid, size)` pairs are detected at batch
    time, not here, so the caller still sees one entry per file path;
    dedup happens against the wire-shape objects.
    """
    from outo_models_cli.errors import BadResponseError

    small: list[Path] = []
    large: list[LargeFile] = []
    for f in files:
        size = f.stat().st_size
        if size <= cap_bytes:
            small.append(f)
            continue
        oid, observed = _sha256_of(f)
        if observed != size:
            # Defensive: `stat().st_size` could race with concurrent
            # writers. Surface as `BadResponseError`-shaped failure so
            # the user sees a clean English line, not a Python traceback.
            raise BadResponseError(
                f"Size of {f} changed during hashing ({observed} != {size}); retry.",
            )
        large.append(LargeFile(path=f, oid=oid, size=size))
    large.sort(key=lambda lf: lf.size, reverse=True)
    return Partition(small=small, large=large)


# ---------------------------------------------------------------------------
# Deduplication — identical (oid, size) pairs collapse to one batch entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DedupMap:
    """Mapping from a deduped `(oid, size)` back to the files that need it.

    The server's batch response carries one entry per `(oid, size)`. If
    the user happens to have two different paths with identical content
    (a common case in model repos that bundle the same checkpoint twice
    under different names), only one PUT is required and the pointer
    text is reused at commit time.
    """

    key: tuple[str, int]
    paths: tuple[Path, ...]


def dedupe_objects(items: list[LargeFile]) -> tuple[list[dict[str, Any]], list[DedupMap]]:
    """Collapse duplicate `(oid, size)` items; return (batch_entries, path_maps).

    `batch_entries` is the list of `{"oid", "size"}` dicts to send in
    the LFS batch body — order matches the first occurrence in `items`
    so the partition-time ordering survives. `path_maps` carries the
    full set of paths that need each pointer text at commit time.
    """
    seen: dict[tuple[str, int], int] = {}
    entries: list[dict[str, Any]] = []
    path_lists: list[list[Path]] = []

    for item in items:
        key = (item.oid, item.size)
        idx = seen.get(key)
        if idx is None:
            seen[key] = len(entries)
            entries.append({"oid": item.oid, "size": item.size})
            path_lists.append([item.path])
        else:
            path_lists[idx].append(item.path)

    maps = [DedupMap(key=key, paths=tuple(path_lists[idx])) for key, idx in seen.items()]
    return entries, maps


def pointers_for(large: list[LargeFile]) -> dict[str, bytes]:
    """Return a `{oid: pointer_text}` mapping for every deduped object.

    The mapping is keyed on oid only because dedupe means a single
    pointer text covers every file that hashes to that oid; the caller
    expands it back to one entry per path when building the multipart
    request.
    """
    out: dict[str, bytes] = {}
    for item in large:
        out.setdefault(item.oid, pointer_text(item.oid, item.size))
    return out


__all__ = [
    "HASH_CHUNK_BYTES",
    "LFS_CONTENT_TYPE",
    "PUT_CHUNK_BYTES",
    "DedupMap",
    "LargeFile",
    "Partition",
    "dedupe_objects",
    "partition_files",
    "pointer_text",
    "pointers_for",
]
