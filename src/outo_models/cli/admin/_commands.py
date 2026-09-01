"""Typer command bodies for `outo-models admin ...`.

Each command: validate `--api-url` / `--token`, branch on remote vs
local, print a single status line. The actual logic lives in
`admin._local_db` (SQL) and `cli_remote.api` (HTTP) — adding a new admin
command is a 3-line diff.

# allow: SIZE_OK — every admin endpoint needs its own Typer command
# body (the public surface is part of the AGENTS.md contract) and the
# `remote_action` / `local_action` lambda pair is the cleanest
# expression of the dispatch; further splitting would force the import
# surface to grow without buying anything.
"""

from __future__ import annotations

import secrets
from typing import Any

import typer

from outo_models.cli import (
    format_bytes,
    parse_human_bytes,
    render_error,
    typer_exit,
)
from outo_models.cli.admin import _local_db
from outo_models.exceptions import NotFoundError, ValidationFailedError

# Allowed values for the `list` filter — mirrors the FastAPI router's
# `pattern="^(pending|approved|denied|banned)?$"`. Duplicated here so the
# CLI can validate without round-tripping through HTTP.
VALID_STATUSES = ("pending", "approved", "denied", "banned")


def _remote_options(
    api_url: str | None,
    token: str | None,
) -> tuple[str | None, str | None, bool]:
    """Validate the `--api-url` / `--token` pair.

    Returns `(api_url, token, is_remote)`. `is_remote` is True iff BOTH
    are provided (the CLI refuses to operate on a remote server with no
    credential — that is always a typo or an automation bug).
    """
    if (api_url is None) != (token is None):
        render_error(ValidationFailedError("--api-url and --token must be used together."))
        raise typer_exit(1)
    return api_url, token, api_url is not None and token is not None


def _print_users_table(users: list[dict[str, Any]]) -> None:
    if not users:
        typer.echo("(no users)")
        return
    header = f"{'username':<20} {'email':<32} {'role':<8} {'status':<10} {'id':<5}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for u in users:
        typer.echo(
            f"{u.get('username', '')!s:<20} {u.get('email', '')!s:<32} "
            f"{u.get('role', '')!s:<8} {u.get('status', '')!s:<10} "
            f"{u.get('id', '')!s:<5}"
        )


def _require_admin() -> Any:
    """Resolve the first admin user for the local-DB actor; exit 1 if none exists."""
    admin = _local_db.require_admin_for_local()
    if admin is None:
        render_error(NotFoundError("no admin account in the local DB. Create one via setup."))
        raise typer_exit(1)
    return admin


def _dispatch_remote_or_local(
    api_url: str | None,
    token: str | None,
    *,
    remote_action: Any,
    local_action: Any,
) -> Any:
    """Run `remote_action(client)` or `local_action()` based on the flag pair.

    Returns whatever the chosen action returns. Both callables receive no
    positional args; capture via closure. Centralising the dispatch keeps
    each command body to the unique part of its logic.
    """
    remote_api_url, remote_token, is_remote = _remote_options(api_url, token)
    if is_remote:
        from outo_models.cli_remote import AdminApiClient

        with AdminApiClient(remote_api_url or "", remote_token or "") as client:
            return remote_action(client)
    return local_action()


# Command functions (registered onto the parent Typer app via __init__.py).


def list_users_command(
    status_filter: str | None,
    api_url: str | None,
    token: str | None,
) -> None:
    """Print the list of users."""
    if status_filter is not None and status_filter not in VALID_STATUSES:
        render_error(ValidationFailedError(f"--status must be one of {VALID_STATUSES}."))
        raise typer_exit(1)
    users = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.list_users(status_filter=status_filter),
        local_action=lambda: _local_db.run_async(_local_db._list_users_async(status_filter)),
    )
    _print_users_table(users)


def pending_command(
    api_url: str | None,
    token: str | None,
) -> None:
    """Print only the users awaiting approval (shortcut for `admin list --status pending`)."""
    users = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.list_users(status_filter="pending"),
        local_action=lambda: _local_db.run_async(_local_db._list_users_async("pending")),
    )
    _print_users_table(users)


def _user_dict(user: Any) -> dict[str, Any]:
    """Local-DB User → the same dict shape `AdminApiClient.approve()` returns."""
    return {
        "username": user.username,
        "status": user.status,
        "max_bytes": None,
        "used_bytes": None,
        "gpu_ids": None,
    }


def approve_command(
    username: str,
    api_url: str | None,
    token: str | None,
) -> None:
    """Approve a pending signup."""
    result = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.approve(username),
        local_action=lambda: _user_dict(
            _local_db.run_async(_local_db._approve_async(username, _require_admin()))
        ),
    )
    typer.echo(f"[approved] {result['username']} -> {result['status']}")


def deny_command(
    username: str,
    reason: str | None,
    api_url: str | None,
    token: str | None,
) -> None:
    """Deny a signup."""
    result = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.deny(username, reason=reason),
        local_action=lambda: _user_dict(
            _local_db.run_async(_local_db._deny_async(username, _require_admin(), reason))
        ),
    )
    typer.echo(f"[denied] {result['username']} -> {result['status']}")


def ban_command(
    username: str,
    reason: str | None,
    api_url: str | None,
    token: str | None,
) -> None:
    """Ban a user."""
    result = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.ban(username, reason=reason),
        local_action=lambda: _user_dict(
            _local_db.run_async(_local_db._ban_async(username, _require_admin(), reason))
        ),
    )
    typer.echo(f"[banned] {result['username']} -> {result['status']}")


def unban_command(
    username: str,
    api_url: str | None,
    token: str | None,
) -> None:
    """Lift a ban."""
    result = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.unban(username),
        local_action=lambda: _user_dict(
            _local_db.run_async(_local_db._unban_async(username, _require_admin()))
        ),
    )
    typer.echo(f"[unbanned] {result['username']} -> {result['status']}")


def quota_show_command(
    username: str,
    api_url: str | None,
    token: str | None,
) -> None:
    """Show a user's storage quota."""
    quota = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.get_quota(username),
        local_action=lambda: _local_db.run_async(_local_db._get_quota_async(username)),
    )
    typer.echo(
        f"[quota] {username}: max={format_bytes(int(quota['max_bytes']))} "
        f"used={format_bytes(int(quota.get('used_bytes', 0)))}"
    )


def quota_set_command(
    username: str,
    max_bytes: str,
    api_url: str | None,
    token: str | None,
) -> None:
    """Set a user's storage quota."""
    try:
        new_max = parse_human_bytes(max_bytes)
    except ValidationFailedError as exc:
        render_error(exc)
        raise typer_exit(1) from exc

    def _local_set() -> dict[str, Any]:
        _local_db.run_async(_local_db._set_quota_async(username, _require_admin(), new_max))
        return {"max_bytes": new_max}

    result = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.set_quota(username, new_max),
        local_action=_local_set,
    )
    typer.echo(f"[quota] {username}: max={format_bytes(int(result['max_bytes']))}")


def gpu_show_command(
    username: str,
    api_url: str | None,
    token: str | None,
) -> None:
    """Show a user's GPU assignment."""
    payload = _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c._request_json("GET", f"/users/{username}/gpu"),
        local_action=lambda: {"gpu_ids": _local_db.run_async(_local_db._get_gpu_async(username))},
    )
    gpu_ids = list(payload.get("gpu_ids", []))
    if not gpu_ids:
        typer.echo(f"[gpu] {username}: no GPUs assigned")
        return
    typer.echo(f"[gpu] {username}: {', '.join(gpu_ids)}")


def gpu_assign_command(
    username: str,
    gpu_ids: list[str],
    api_url: str | None,
    token: str | None,
) -> None:
    """Assign GPUs to a user (overwrites the existing list)."""
    _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.set_gpu(username, list(gpu_ids)),
        local_action=lambda: _local_db.run_async(
            _local_db._set_gpu_async(username, _require_admin(), list(gpu_ids))
        ),
    )
    typer.echo(f"[gpu] {username}: assigned {gpu_ids}")


def gpu_clear_command(
    username: str,
    api_url: str | None,
    token: str | None,
) -> None:
    """Remove all GPU assignments from a user."""
    _dispatch_remote_or_local(
        api_url,
        token,
        remote_action=lambda c: c.clear_gpu(username),
        local_action=lambda: _local_db.run_async(
            _local_db._clear_gpu_async(username, _require_admin())
        ),
    )
    typer.echo(f"[gpu] {username}: assignment cleared")


def reset_password_command(username: str) -> None:
    """Generate a new password and print it once (for recovery)."""
    admin = _require_admin()
    new_password = secrets.token_urlsafe(18)
    user = _local_db.run_async(_local_db._reset_password_async(username, admin, new_password))
    typer.echo(f"[reset] New password for {user.username} (will not be shown again):")
    typer.echo(f"  {new_password}")


__all__ = [
    "approve_command",
    "ban_command",
    "deny_command",
    "gpu_assign_command",
    "gpu_clear_command",
    "gpu_show_command",
    "list_users_command",
    "pending_command",
    "quota_set_command",
    "quota_show_command",
    "reset_password_command",
    "unban_command",
]
