"""`omc repo ...` — manage repositories on the configured server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from outo_models_cli import api
from outo_models_cli.commands._shared import (
    _errprint,
    _human_bytes,
    _resolve_target,
    _validate_kind,
    _visibility,
    console,
)
from outo_models_cli.errors import (
    AuthRequiredError,
    OmcError,
)

if TYPE_CHECKING:
    from outo_models_cli.config import Store

repo_app = typer.Typer(help="Manage repositories.", no_args_is_help=True)


@repo_app.command("create")
def repo_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Repository name.")],
    kind: Annotated[str, typer.Option(help="One of model, dataset, space.")] = "model",
    public: Annotated[
        bool,
        typer.Option("--public", help="Make the repo public (overrides --private)."),
    ] = False,
    private: Annotated[
        bool,
        typer.Option("--private", help="Make the repo private (default)."),
    ] = False,
    description: Annotated[
        str | None,
        typer.Option(help="One-line description stored on the repo."),
    ] = None,
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """Create a new repository on the target server."""
    store: Store = ctx.obj["store"]
    try:
        kind = _validate_kind(kind)
        visibility = _visibility(public, private)
        _base_url, client = _resolve_target(store, server=server)
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        with client:
            summary = api.create_repo(
                client,
                name=name,
                kind=kind,
                visibility=visibility,
                description=description,
            )
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    console.print(
        f"Created {summary.visibility} {summary.kind} [bold]{summary.owner}/{summary.name}[/bold].",
    )
    console.print(f"Clone URL: {summary.clone_url}")


@repo_app.command("delete")
def repo_delete(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="Repository as `<owner>/<name>`.")],
    kind: Annotated[
        str,
        typer.Option(help="Repo kind: model, dataset, or space."),
    ] = "model",
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """Delete a repository (owner or admin only)."""
    store: Store = ctx.obj["store"]
    if "/" not in repo:
        _errprint("Argument must be `<owner>/<name>`.")
        raise typer.Exit(code=1)
    owner, name = repo.split("/", 1)
    if not yes:
        typer.confirm(
            f"Delete {kind} {owner}/{name}? This cannot be undone.",
            abort=True,
        )
    try:
        kind = _validate_kind(kind)
        _base_url, client = _resolve_target(store, server=server)
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        with client:
            api.delete_repo(client, owner=owner, name=name, kind=kind)
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    console.print(f"Deleted {kind} {owner}/{name}.")


@repo_app.command("list")
def repo_list(
    ctx: typer.Context,
    owner: Annotated[str | None, typer.Option(help="Limit to one owner's repos.")] = None,
    kind: Annotated[
        str | None,
        typer.Option(help="Filter by kind: model, dataset, or space."),
    ] = None,
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """List repositories on the target server."""
    store: Store = ctx.obj["store"]
    try:
        kind = _validate_kind(kind) if kind else None
        _base_url, client = _resolve_target(store, server=server)
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        with client:
            rows = api.list_repos(client, kind=kind, owner=owner)
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    if not rows:
        console.print("No repositories found.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Owner")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Visibility")
    table.add_column("Size")
    table.add_column("Description")
    for row in rows:
        size = _human_bytes(row.size_bytes) if row.size_bytes else "0 B"
        table.add_row(
            row.owner,
            row.name,
            row.kind,
            row.visibility,
            size,
            row.description or "",
        )
    console.print(table)


__all__ = ["repo_app"]
