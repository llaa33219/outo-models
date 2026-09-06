"""`omc ls <owner>/<name>` — list one directory of a repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from outo_models_cli import api
from outo_models_cli.commands._shared import (
    _errprint,
    _human_bytes,
    _parse_owner_repo,
    _resolve_target,
    console,
)
from outo_models_cli.errors import (
    AuthRequiredError,
    OmcError,
)

if TYPE_CHECKING:
    from outo_models_cli.config import Store


def ls_command(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="Repository as `<owner>/<name>`.")],
    path: Annotated[
        str,
        typer.Option(help="Directory inside the repo to list (default: root)."),
    ] = "",
    revision: Annotated[
        str,
        typer.Option(help="Branch, tag, or commit SHA (default: main)."),
    ] = "main",
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """List one directory of a repository."""
    store: Store = ctx.obj["store"]
    try:
        owner, name = _parse_owner_repo(repo)
        _base_url, client = _resolve_target(store, server=server)
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        with client:
            rows = api.list_files(
                client,
                owner=owner,
                name=name,
                path=path,
                revision=revision,
            )
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    if not rows:
        console.print("(empty directory)")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Size")
    for entry in rows:
        if entry.kind == "dir":
            size = "-"
        elif entry.size_bytes is None:
            size = "?"
        elif entry.size_bytes == 0:
            size = "0 B"
        else:
            size = _human_bytes(entry.size_bytes)
        table.add_row(entry.name, entry.kind, size)
    console.print(table)


__all__ = ["ls_command"]
