"""Recursive, resumable downloader.

Why one package owns the whole flow (not the CLI command):

    * The CLI command renders progress bars; the downloader is the only
      layer that knows chunk sizes, byte counts, and resume semantics.
    * A single transport (`httpx.AsyncClient`) avoids the concurrency
      trap of running sync calls inside a thread pool: one connection
      pool, one auth header, one close path.
    * Streaming integrity checks (ETag / Content-Length) belong with
      the byte-level logic; mixing them into the CLI renderer would
      scatter the wire contract across files.

The public entry point is `outo_models_cli.downloader.download_repo` —
a coroutine that walks the remote tree, schedules parallel file
transfers under an asyncio semaphore, and yields per-file outcomes the
CLI renderer can attach Rich bars to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from outo_models_cli import api
from outo_models_cli.downloader.transfer import FileOutcome, transfer_one
from outo_models_cli.downloader.walk import walk_repo_async


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    """All knobs that change the downloader behavior.

    Centralising them in a frozen dataclass keeps the download coroutine
    signature short (the function takes one config object) and lets
    tests pin every value at once. Defaults match the values the CLI
    commands set when invoked from `omc download`.
    """

    revision: str = "main"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    local_dir: Path = Path(".")
    max_workers: int = 8


def build_progress() -> Progress:
    """Return the per-file Rich progress bar group.

    Exposed for the CLI command so the same columns render in tests as
    in production. A custom column set (instead of the default
    `Progress()`) gives us a fixed-width `description` cell, which
    keeps the multi-file bars aligned when filenames vary in length.
    """
    return Progress(
        TextColumn("[bold blue]{task.description}[/bold blue]", justify="left"),
        BarColumn(bar_width=40),
        DownloadColumn(),
        TransferSpeedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        expand=True,
    )


ProgressCallback = Callable[[Progress, Any], None]
"""Hook the CLI uses to bind a per-file task. Kept as a thin callback
so the downloader never imports the CLI renderer directly."""


async def download_repo(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    owner: str,
    name: str,
    config: DownloadConfig,
    on_progress: Callable[[str], None] | None = None,
) -> list[FileOutcome]:
    """Walk `owner/name` and stream every file under `config.local_dir`.

    Returns one `FileOutcome` per file, in the order the tree walk
    produced (depth-first, alphabetical inside each directory). The
    caller is expected to run this coroutine under
    `asyncio.run(...)` and is free to wrap it with their own progress
    rendering — `on_progress` is a per-file completion notification the
    CLI uses to print a one-line summary without subscribing to the
    Rich bar stream.
    """
    files = await walk_repo_async(
        client,
        owner=owner,
        name=name,
        revision=config.revision,
        include=config.include,
        exclude=config.exclude,
    )

    config.local_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, config.max_workers))

    progress = build_progress()
    outcomes: list[FileOutcome] = []
    with progress:
        tasks: list[asyncio.Future[FileOutcome]] = []

        async def _bound(entry: api.FileEntry) -> FileOutcome:
            target = config.local_dir / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            task_id = progress.add_task(
                entry.path,
                total=entry.size_bytes,
                completed=0,
            )
            async with semaphore:
                outcome = await transfer_one(
                    client,
                    owner=owner,
                    name=name,
                    revision=config.revision,
                    file=entry,
                    target=target,
                    progress=progress,
                    task_id=task_id,
                )
            progress.remove_task(task_id)
            return outcome

        for entry in files:
            tasks.append(asyncio.ensure_future(_bound(entry)))

        for coro in asyncio.as_completed(tasks):
            outcome = await coro
            outcomes.append(outcome)
            if on_progress is not None:
                on_progress(outcome.path)

    return outcomes


__all__ = [
    "DownloadConfig",
    "FileOutcome",
    "ProgressCallback",
    "build_progress",
    "download_repo",
]
