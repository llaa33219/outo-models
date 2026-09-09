"""`omc auth ...` — manage stored credentials.

Four subcommands mirror the HF CLI surface:

    * `login --server <url> [--token PAT] [--set-default]`
    * `logout [--server <url>]`
    * `whoami [--server <url>]`
    * `status`

All four share the `_auth_resolve` helper, which handles URL
normalization and the env-var overrides the store layer exposes.
"""

from __future__ import annotations

import getpass
import os
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from outo_models_cli import api, config
from outo_models_cli.commands._shared import (
    _errprint,
    console,
)
from outo_models_cli.errors import (
    AuthInvalidError,
    AuthRequiredError,
    ConfigError,
    OmcError,
)

if TYPE_CHECKING:
    from outo_models_cli.config import Store

auth_app = typer.Typer(help="Manage stored credentials.", no_args_is_help=True)


def _prompt_token(url: str) -> str:
    """Prompt for a PAT (masked) on the controlling terminal."""
    return getpass.getpass(f"Token for {url}: ")


def _resolve_target_url(
    store: Store,
    *,
    requested: str | None,
) -> str:
    """Normalize and validate the user-supplied server URL.

    Falls back to the env override (`OMC_SERVER`) and then the configured
    default so `omc auth logout` without `--server` operates on the
    current target.
    """
    raw = requested or os.environ.get("OMC_SERVER") or store.default_server
    if not raw:
        raise AuthRequiredError(
            "No server given. Pass --server <url> or run `omc auth login --server <url>` first.",
        )
    return config.normalize_server_url(raw)


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    server: Annotated[
        str | None,
        typer.Option(help="Server URL (default: pick the first configured server)."),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="PAT to store verbatim (skips the masked prompt). Use only in scripts.",
        ),
    ] = None,
    set_default: Annotated[
        bool,
        typer.Option(
            "--set-default",
            help="Pin this server as the default target after login.",
        ),
    ] = False,
) -> None:
    """Log in to a server: verify the PAT and store it."""
    store: Store = ctx.obj["store"]
    try:
        url = _resolve_target_url(store, requested=server)
    except (OmcError, AuthRequiredError, ConfigError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    pat = token or _prompt_token(url)
    if not pat:
        _errprint("Empty token; aborting.")
        raise typer.Exit(code=1)

    # Verify before persisting — a wrong token must never end up on disk.
    try:
        with api.with_client(url, pat) as client:
            identity = api.me(client)
    except AuthInvalidError:
        _errprint("Server rejected the token. Re-run with a valid PAT.")
        raise typer.Exit(code=1) from None
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    new_store = store.with_login(url, pat, username=identity.username)
    if set_default:
        new_store = new_store.with_default(url)
    config.save_store(new_store, ctx.obj["config_path"])

    source = "OMC_TOKEN env var" if os.environ.get("OMC_TOKEN") == pat else "stored credential"
    console.print(
        f"Logged in to {url} as [bold]{identity.username}[/bold] (role: {identity.role}); "
        f"token source: {source}.",
    )


@auth_app.command("logout")
def auth_logout(
    ctx: typer.Context,
    server: Annotated[
        str | None,
        typer.Option(help="Server to log out (default: the configured default)."),
    ] = None,
) -> None:
    """Remove the stored credential for one server."""
    store: Store = ctx.obj["store"]
    try:
        url = _resolve_target_url(store, requested=server)
    except (OmcError, AuthRequiredError, ConfigError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    if url not in store.servers:
        _errprint(f"No stored credential for {url}.")
        raise typer.Exit(code=1)

    new_store = store.without(url)
    config.save_store(new_store, ctx.obj["config_path"])
    console.print(f"Removed stored credential for {url}.")


@auth_app.command("whoami")
def auth_whoami(
    ctx: typer.Context,
    server: Annotated[
        str | None,
        typer.Option(help="Server to query (default: the configured default)."),
    ] = None,
) -> None:
    """Print the authenticated user, server, and token source."""
    store: Store = ctx.obj["store"]
    try:
        url, token = store.resolve(server)
    except AuthRequiredError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        with api.with_client(url, token) as client:
            identity = api.me(client)
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    source = "OMC_TOKEN env var" if os.environ.get("OMC_TOKEN") else "stored credential"
    table = Table(show_header=False, box=None)
    table.add_row("server", url)
    table.add_row("username", identity.username)
    table.add_row("role", identity.role)
    table.add_row("token source", source)
    console.print(table)


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """List every configured server and mark the default."""
    store: Store = ctx.obj["store"]
    if not store.servers:
        console.print("No servers configured. Run `omc auth login --server <url>`.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Server")
    table.add_column("Default")
    for url in store.list_servers():
        is_default = "yes" if url == store.default_server else ""
        table.add_row(url, is_default)
    console.print(table)


__all__ = ["auth_app"]
