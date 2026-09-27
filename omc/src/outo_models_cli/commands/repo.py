"""`omc repo ...` — manage repositories on the configured server.

The long-form help (PURPOSE / USAGE / EXAMPLES / EXIT CODES / NOTES)
lives in `commands/_help_text/_repo.py` so it stays consistent with
the test markers. Docstrings stay as the one-line summary Typer
renders above the auto-generated table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from outo_models_cli import api
from outo_models_cli.commands._help_text import (
    REPO_APP_HELP,
    REPO_CREATE_EPILOG,
    REPO_DELETE_EPILOG,
    REPO_LIST_EPILOG,
)
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

repo_app = typer.Typer(help=REPO_APP_HELP, no_args_is_help=True)


@repo_app.command("create", epilog=REPO_CREATE_EPILOG)
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
    """PURPOSE
    Create a new repository on the resolved server. After creation the
    CLI prints the new repo's `owner/name`, visibility, kind, and
    git-style clone URL.

    USAGE
    omc repo create <name> [--kind model|dataset|space] [--public|--private]
                         [--description <text>] [--server <url>]"""
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


@repo_app.command("delete", epilog=REPO_DELETE_EPILOG)
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
    """PURPOSE
    Permanently delete a repository on the resolved server. The owner
    or any account with the `admin` role may delete. The operation
    is irreversible - the git history, all revisions, and every LFS
    object are removed.

    USAGE
    omc repo delete <owner>/<name> [--kind model|dataset|space]
                              [--yes] [--server <url>]"""
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


@repo_app.command("list", epilog=REPO_LIST_EPILOG)
def repo_list(
    ctx: typer.Context,
    owner: Annotated[str | None, typer.Option(help="Limit to one owner's repos.")] = None,
    kind: Annotated[
        str | None,
        typer.Option(help="Filter by kind: model, dataset, or space."),
    ] = None,
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """PURPOSE
    List every repository on the resolved server, with optional
    filters on owner and kind. The output is a Rich table; pipe it
    through `column -t -s '|'` or convert with `jq` for machine use.

    USAGE
    omc repo list [--owner <name>] [--kind model|dataset|space]
                  [--server <url>]"""
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
