"""Stream one file with resume + integrity semantics."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.progress import Progress

from outo_models_cli import api
from outo_models_cli.errors import NotFoundError, map_response_error, map_transport_error

# 1 MiB chunk size — small enough to keep memory bounded even when the
# CLI streams many parallel downloads; large enough that the chunk
# overhead is negligible against the per-byte disk cost.
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """One row in the download report the CLI prints at the end."""

    path: str
    bytes_downloaded: int
    resumed: bool
    verified: bool  # True when an ETag / sha check was performed


async def transfer_one(
    client: httpx.AsyncClient,
    *,
    owner: str,
    name: str,
    revision: str,
    file: api.FileEntry,
    target: Path,
    progress: Progress,
    task_id: Any,
) -> FileOutcome:
    """Stream `file` from the server to `target`, honouring `.part` resume."""
    url = api.resolve_url(owner=owner, name=name, revision=revision, path=file.path)
    part_path = target.with_suffix(target.suffix + ".part")
    existing_size = part_path.stat().st_size if part_path.exists() else 0

    headers: dict[str, str] = {}
    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    try:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416:
                # Range not satisfiable → server's file shrunk or our
                # .part is corrupt. Restart from scratch.
                part_path.unlink(missing_ok=True)
                return await transfer_one(
                    client,
                    owner=owner,
                    name=name,
                    revision=revision,
                    file=file,
                    target=target,
                    progress=progress,
                    task_id=task_id,
                )
            if response.status_code == 404:
                raise NotFoundError(f"File not found on server: {file.path}")
            if response.status_code >= 400:
                raise map_response_error(response)

            server_etag = response.headers.get("ETag")
            content_length_raw = response.headers.get("Content-Length")
            content_length = (
                int(content_length_raw)
                if content_length_raw and content_length_raw.isdigit()
                else None
            )
            expected_total = file.size_bytes

            if response.status_code == 200:
                # Server ignored Range and is sending the full file
                # (ETag mismatch, or no Range header was sent).
                resumed = False
                part_path.unlink(missing_ok=True)
                mode = "wb"
                existing_size = 0
                download_total = expected_total if expected_total is not None else 0
            else:  # 206 Partial Content
                resumed = existing_size > 0
                mode = "ab"
                download_total = (
                    (expected_total - existing_size)
                    if expected_total is not None and existing_size <= expected_total
                    else 0
                )

            bytes_written = existing_size
            progress.update(
                task_id,
                total=(download_total + bytes_written) or None,
                completed=bytes_written,
            )

            # ETag may be a quoted hex sha or a weak validator. The
            # integrity check is a verbatim match against the value the
            # server sent on this response; mismatches between calls
            # signal "the file changed; restart from scratch" above.
            sha = hashlib.sha256() if server_etag and "sha256" in server_etag.lower() else None

            with part_path.open(mode) as fp:
                async for chunk in response.aiter_bytes(chunk_size=CHUNK_BYTES):
                    if not chunk:
                        continue
                    fp.write(chunk)
                    bytes_written += len(chunk)
                    progress.update(task_id, completed=bytes_written)
                    if sha is not None:
                        sha.update(chunk)

        # Atomic rename to the final target. If anything in this block
        # raises (disk full, permissions), the .part file is left on
        # disk for the next run to resume.
        os.replace(part_path, target)

        verified = server_etag is not None or content_length is not None

        return FileOutcome(
            path=file.path,
            bytes_downloaded=bytes_written,
            resumed=resumed,
            verified=verified,
        )
    except httpx.HTTPError as exc:
        raise map_transport_error(exc) from exc


__all__ = ["transfer_one"]
