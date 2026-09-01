"""`outo-models admin ...` — user / quota / GPU management.

Two execution paths share this single Typer sub-app:

    * **Local DB** (default): the operator runs the CLI on the server host
      where the SQLite file lives. Commands delegate to `admin._local_db`.
    * **Remote admin** (`--api-url` / `--token`): the operator points the
      CLI at a running server and the commands proxy to the
      `/api/admin/*` endpoints via `outo_models.cli_remote.AdminApiClient`.

The contract per command is identical between paths; the only thing that
differs is whether the data goes through `admin._local_db` or a HTTP
request. Both paths emit the same CLI output for the operator.

`reset-password` is local-only by design — regenerating a password on a
remote server would expose it on the wire and there is no admin endpoint
for it. The CLI prints the new password ONCE; the operator must capture
it before the command returns.

This `__init__.py` is the *only* file in the package that the parent
`outo_models.cli.main` imports. It builds the Typer `admin_app`,
registers every command from `_commands`, and re-exports the surface.
"""

from __future__ import annotations

import typer

from outo_models.cli.admin import _commands

admin_app = typer.Typer(
    name="admin",
    help="Manage users, quotas, and GPUs",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
quota_app = typer.Typer(name="quota", help="Manage per-user storage quotas", no_args_is_help=True)
gpu_app = typer.Typer(name="gpu", help="Manage per-user GPU assignments", no_args_is_help=True)
admin_app.add_typer(quota_app, name="quota")
admin_app.add_typer(gpu_app, name="gpu")


def _url_and_token(
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> tuple[str | None, str | None]:
    return api_url, token


@admin_app.command("list")
def _list(
    status_filter: str | None = typer.Option(
        None,
        "--status",
        help=f"Status filter ({'|'.join(_commands.VALID_STATUSES)}).",
    ),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.list_users_command(status_filter, api_url, token)


@admin_app.command("pending")
def _pending(
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.pending_command(api_url, token)


@admin_app.command("approve")
def _approve(
    username: str = typer.Argument(..., help="Username to approve"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.approve_command(username, api_url, token)


@admin_app.command("deny")
def _deny(
    username: str = typer.Argument(..., help="Username to deny"),
    reason: str | None = typer.Option(None, "--reason", help="Reason for denial"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.deny_command(username, reason, api_url, token)


@admin_app.command("ban")
def _ban(
    username: str = typer.Argument(..., help="Username to ban"),
    reason: str | None = typer.Option(None, "--reason", help="Reason for ban"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.ban_command(username, reason, api_url, token)


@admin_app.command("unban")
def _unban(
    username: str = typer.Argument(..., help="Username to unban"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.unban_command(username, api_url, token)


@quota_app.command("show")
def _quota_show(
    username: str = typer.Argument(..., help="Target username"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.quota_show_command(username, api_url, token)


@quota_app.command("set")
def _quota_set(
    username: str = typer.Argument(..., help="Target username"),
    max_bytes: str = typer.Argument(..., help="Max bytes (e.g. 10GiB)"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.quota_set_command(username, max_bytes, api_url, token)


@gpu_app.command("show")
def _gpu_show(
    username: str = typer.Argument(..., help="Target username"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.gpu_show_command(username, api_url, token)


@gpu_app.command("assign")
def _gpu_assign(
    username: str = typer.Argument(..., help="Target username"),
    gpu_ids: list[str] = typer.Argument(..., help="GPU IDs to assign (space-separated)"),  # noqa: B008
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.gpu_assign_command(username, gpu_ids, api_url, token)


@gpu_app.command("clear")
def _gpu_clear(
    username: str = typer.Argument(..., help="Target username"),
    api_url: str | None = typer.Option(None, "--api-url", help="Remote server URL"),
    token: str | None = typer.Option(None, "--token", help="Remote server PAT"),
) -> None:
    _commands.gpu_clear_command(username, api_url, token)


@admin_app.command("reset-password")
def _reset_password(
    username: str = typer.Argument(..., help="Target username"),
) -> None:
    _commands.reset_password_command(username)


__all__ = ["admin_app"]
