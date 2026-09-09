"""Mixed multipart commit — small files + LFS pointer text in one call.

After the partition step splits the user's files into small (multipart)
and large (LFS) buckets, the LFS objects land in the object store via
the batch + PUT dance, and the repo receives ONE commit whose tree
contains both the small files as bytes and the large files as LFS
pointer text. This module owns the second half: building the multipart
payload that mixes on-disk files and in-memory pointer buffers and
issuing it through `api.upload`.

The split between `api.upload` (the single-purpose multipart helper)
and `commit_mixed` (the LFS-aware variant) keeps the simple path tiny
and isolates the BytesIO-handle plumbing this flow needs. The threshold
for splitting into per-file commits (`_SINGLE_REQUEST_TOTAL_BYTES`) is
the same value the pre-LFS upload command used, so a folder that would
have uploaded in pieces before still does.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import BinaryIO

import httpx

from outo_models_cli import api
from outo_models_cli.api import lfs as lfs_api
from outo_models_cli.commands._shared import _SINGLE_REQUEST_TOTAL_BYTES


def _pointer_handles(
    partition: lfs_api.Partition,
    *,
    file_root: Path,
) -> list[tuple[str, io.BytesIO]]:
    """Materialise one `(display_name, BytesIO)` per large file for the multipart call.

    Identical-content dedup means a single pointer text covers every
    file that hashes to the same oid, but the server expects one
    multipart part per path — so we expand the dedup map back to one
    entry per source path here.
    """
    handles: list[tuple[str, io.BytesIO]] = []
    seen_paths: set[Path] = set()
    idx = 0
    for item in partition.large:
        if item.path in seen_paths:
            continue
        seen_paths.add(item.path)
        label = api.display_filename(str(item.path), file_root=file_root, idx=idx)
        idx += 1
        buf = io.BytesIO(api.pointer_text(item.oid, item.size))
        buf.name = label
        handles.append((label, buf))
    return handles


def _path_in_repo_for(file: Path, *, root: Path, default: str) -> str:
    """Compute the destination subdirectory for one file in a folder upload.

    The server applies `path` to every filename in the multipart batch,
    so when the user uploads a folder of files we want the *common
    parent directory* (not the file's full relative path) to land under
    `path_in_repo`. Files at the root go straight under the user's
    `path_in_repo` verbatim.
    """
    try:
        rel_parent = file.resolve().relative_to(root.resolve()).parent
    except ValueError:
        return default
    if rel_parent == Path("."):
        return default
    base = default.rstrip("/")
    return f"{base}/{rel_parent.as_posix()}" if base else rel_parent.as_posix()


def _multipart_upload(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    handles: list[tuple[Path, BinaryIO]],
    path_in_repo: str,
    message: str | None,
    file_root: Path,
) -> api.UploadResult:
    """Drive `api.upload` with a list of `(path, handle)` pairs.

    A thin wrapper that exists so the mixed (file + pointer) case can
    reuse `api.upload`'s error-mapping without having to inline a
    second copy of the multipart body construction.
    """
    paths = [p for p, _ in handles]
    file_handles = [h for _, h in handles]
    return api.upload(
        client,
        owner=owner,
        name=name,
        files=paths,
        path_in_repo=path_in_repo,
        message=message,
        file_root=file_root,
        file_handles=file_handles,
    )


def commit_mixed(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    partition: lfs_api.Partition,
    file_root: Path,
    path_in_repo: str,
    message: str | None,
) -> api.UploadResult:
    """Issue the final multipart commit, mixing small files and pointer buffers.

    Files larger than `_SINGLE_REQUEST_TOTAL_BYTES` are split into
    per-file commits to avoid tying up the server on a single request.
    The same threshold was used in the previous (pre-LFS) upload flow
    and stays untouched here.
    """
    pointer_handles = _pointer_handles(partition, file_root=file_root)
    small_files = list(partition.small)
    opened_files: list[BinaryIO] = []
    try:
        for p in small_files:
            opened_files.append(p.open("rb"))
        all_handles: list[tuple[Path, BinaryIO]] = [
            (p, h) for p, h in zip(small_files, opened_files, strict=True)
        ] + [(Path(label), buf) for label, buf in pointer_handles]
        total_bytes = sum(p.stat().st_size for p in small_files) + sum(
            len(buf.getvalue()) for _, buf in pointer_handles
        )

        if total_bytes < _SINGLE_REQUEST_TOTAL_BYTES:
            return _multipart_upload(
                client,
                owner=owner,
                name=name,
                handles=all_handles,
                path_in_repo=path_in_repo,
                message=message,
                file_root=file_root,
            )

        last_sha = ""
        last_files: list[str] = []
        for p, h in all_handles:
            single = _multipart_upload(
                client,
                owner=owner,
                name=name,
                handles=[(p, h)],
                path_in_repo=_path_in_repo_for(p, root=file_root, default=path_in_repo),
                message=message,
                file_root=file_root,
            )
            last_sha = single.commit_sha
            last_files = single.files
        return api.UploadResult(commit_sha=last_sha, files=last_files, message=message)
    finally:
        for h in opened_files:
            h.close()


__all__ = ["commit_mixed"]
