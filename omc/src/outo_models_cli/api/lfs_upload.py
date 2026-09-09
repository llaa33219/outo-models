"""LFS streaming PUT — multi-gigabyte uploads with byte-level progress.

Each `BatchAction` carries an absolute `href` (same-origin for the
local backend, presigned S3 for the S3 backend) and a `header` map the
client must attach verbatim. The PUT body is the raw object bytes,
streamed from disk in 1 MiB chunks so peak memory is O(chunk) regardless
of file size.

The Rich progress bar updates as the chunks are read off disk; that
matches the bytes-on-the-wire count because httpx forwards each
`bytes` yielded by the generator to the transport as it arrives. A
separate "total" task tracks the sum across all actions so the user
sees one combined ETA rather than N independent bars.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from outo_models_cli.api.lfs import PUT_CHUNK_BYTES
from outo_models_cli.api.lfs_batch import BatchAction
from outo_models_cli.errors import (
    BadResponseError,
    FileTooLargeError,
    map_response_error,
)


class _ProgressLike(Protocol):
    """The narrow surface the upload loop needs from any progress sink.

    Both `rich.progress.Progress` and the no-op `_NullProgress` satisfy
    this, so the typing is honest about the polymorphism without
    dragging in a heavy import-only alias.
    """

    def add_task(self, description: str, *, total: float | None = None) -> TaskID: ...
    def update(self, task_id: TaskID, **kwargs: Any) -> None: ...
    def remove_task(self, task_id: TaskID) -> None: ...


def _put_stream(
    client: httpx.Client,
    *,
    url: str,
    path: Path,
    extra_headers: dict[str, str],
    progress: _ProgressLike,
    task_id: TaskID,
) -> None:
    """Stream `path` into `url` with a Rich progress bar driven off bytes-sent.

    httpx accepts an iterator of `bytes` for `content=`; the bytes are
    sent in 1 MiB slices so peak memory is O(chunk), independent of
    file size. The progress bar is updated as a side effect of the
    generator so it stays in lockstep with the actual bytes-on-the-wire
    count (verified for a 1.2 GiB file in the integration harness).
    """
    headers = dict(extra_headers)
    if "Content-Length" not in headers and "content-length" not in {k.lower() for k in headers}:
        # Server's PUT handler uses Content-Length for the streaming cap;
        # announce it so a multi-GB upload is not gated by the cap on
        # the first chunk that arrives.
        size = path.stat().st_size
        headers["Content-Length"] = str(size)
    headers.setdefault("Content-Type", "application/octet-stream")

    def _chunk_iter() -> Iterable[bytes]:
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(PUT_CHUNK_BYTES)
                if not chunk:
                    break
                progress.update(task_id, advance=len(chunk))
                yield chunk

    response = client.put(url, content=_chunk_iter(), headers=headers)
    if response.status_code == 404:
        raise BadResponseError("LFS object endpoint returned 404.")
    if response.status_code == 401:
        raise BadResponseError(
            "Server rejected the LFS upload (HTTP 401). "
            "Check that the configured PAT matches the account owner.",
        )
    if response.status_code == 413:
        raise FileTooLargeError(
            f"LFS object exceeds the server's per-object cap: {path.name}.",
        )
    if response.status_code >= 400:
        raise map_response_error(response)


def upload_objects(
    client: httpx.Client,
    *,
    actions: list[BatchAction],
    paths_by_oid: dict[str, list[Path]],
    show_progress: bool = True,
    progress: _ProgressLike | None = None,
) -> None:
    """Stream-PUT every `BatchAction` from disk with a Rich progress bar.

    `paths_by_oid` maps an oid to its one-or-more source paths; the
    first one is used as the byte source (dedup is exact — same sha256
    means same bytes) AND as the human-facing task label — users track
    "weights-00002-of-00004.safetensors", never a sha prefix. The caller
    MUST ensure `actions` and `paths_by_oid` agree on the oid set.
    `progress` lets tests inject a recorder instead of Rich.
    """
    if not actions:
        return
    # ONE column set for every task row. (The previous layout concatenated
    # a per-object group and a "total" group, so every row rendered both
    # groups and the output read as doubled, unlabeled bars.)
    columns: tuple[Any, ...] = (
        TextColumn("[bold blue]{task.description}[/bold blue]", justify="left"),
        BarColumn(bar_width=32),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
    total_bytes = sum(a.size for a in actions)
    if progress is not None:
        _upload_all(
            progress, client, actions=actions, paths_by_oid=paths_by_oid, total_bytes=total_bytes
        )
        return
    progress_cm: Any = Progress(*columns, expand=True) if show_progress else _NullProgress()
    with progress_cm as active:
        _upload_all(
            active, client, actions=actions, paths_by_oid=paths_by_oid, total_bytes=total_bytes
        )


def _upload_all(
    progress: Any,
    client: httpx.Client,
    *,
    actions: list[BatchAction],
    paths_by_oid: dict[str, list[Path]],
    total_bytes: int,
) -> None:
    overall = progress.add_task(f"Total ({len(actions)} object(s))", total=total_bytes)
    for action in actions:
        sources = paths_by_oid.get(action.oid) or []
        if not sources:
            raise BadResponseError(
                f"LFS action for oid {action.oid} has no source file on disk.",
            )
        task_id = progress.add_task(sources[0].name, total=action.size)
        _put_stream(
            client,
            url=action.href,
            path=sources[0],
            extra_headers=action.header,
            progress=progress,
            task_id=task_id,
        )
        progress.update(task_id, completed=action.size)
        progress.update(overall, advance=action.size)
        progress.remove_task(task_id)


class _NullProgress:
    """No-op progress stand-in used when `--quiet` / non-tty callers skip bars."""

    def __enter__(self) -> _NullProgress:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def add_task(self, description: str, *, total: float | None = None) -> TaskID:
        return TaskID(0)

    def update(self, _task_id: TaskID, **_kwargs: object) -> None:
        return None

    def remove_task(self, _task_id: TaskID) -> None:
        return None


__all__ = ["upload_objects"]
