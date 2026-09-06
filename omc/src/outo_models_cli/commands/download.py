"""`omc download <owner>/<name>` — recursive, resumable repo download."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from outo_models_cli import downloader
from outo_models_cli.commands._shared import (
    _errprint,
    _human_bytes,
    _parse_owner_repo,
    console,
)
from outo_models_cli.errors import (
    AuthRequiredError,
    OmcError,
)
from outo_models_cli.http import build_async_client

if TYPE_CHECKING:
    from outo_models_cli.config import Store


def download_command(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="Repository as `<owner>/<name>`.")],
    revision: Annotated[
        str,
        typer.Option(help="Branch, tag, or commit SHA (default: main)."),
    ] = "main",
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            help="Glob of paths to include (repeatable). Defaults to everything.",
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Glob of paths to exclude (repeatable). Defaults to nothing.",
        ),
    ] = None,
    local_dir: Annotated[
        Path,
        typer.Option(help="Destination directory (default: ./<repo>)."),
    ] = Path("."),
    max_workers: Annotated[
        int,
        typer.Option(help="Parallel download workers (default: 8)."),
    ] = 8,
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """Recursively download a repository to `--local-dir`."""
    store: Store = ctx.obj["store"]
    try:
        owner, name = _parse_owner_repo(repo)
        base_url, token = store.resolve(server)
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    target_dir = local_dir if local_dir != Path(".") else Path(name)
    cfg = downloader.DownloadConfig(
        revision=revision,
        include=include or [],
        exclude=exclude or [],
        local_dir=target_dir,
        max_workers=max_workers,
    )

    client = build_async_client(base_url, token)
    try:
        outcomes = asyncio.run(
            downloader.download_repo(
                client=client,
                base_url=base_url,
                token=token,
                owner=owner,
                name=name,
                config=cfg,
            )
        )
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    total_bytes = sum(o.bytes_downloaded for o in outcomes)
    resumed = sum(1 for o in outcomes if o.resumed)
    console.print(
        f"Downloaded {len(outcomes)} file(s) ({_human_bytes(total_bytes)}) to {target_dir}. "
        f"Resumed: {resumed}.",
    )


__all__ = ["download_command"]
