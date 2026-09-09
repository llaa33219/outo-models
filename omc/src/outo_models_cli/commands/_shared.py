"""Shared helpers for the `omc` Typer commands.

This module owns:

    * the standard consoles (`console`, `err_console`) every command uses,
    * the byte-size and visibility helpers,
    * the `owner/name` parser,
    * the `Store → (base_url, httpx.Client)` resolver,
    * the local-path collector used by `upload`.

The Typer `app` and command registration live in `main.py`. Concentrating
the shared infrastructure here keeps each command module under the
250-LOC ceiling without scattering the error contract across files.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from rich.console import Console

from outo_models_cli import api, config
from outo_models_cli.errors import (
    FileMissingError,
    FileTooLargeError,
    OmcError,
)

if TYPE_CHECKING:
    from outo_models_cli.config import Store

# 100 MiB per-file cap mirrors the server's multipart boundary. Larger
# files go through Git LFS rather than the multipart endpoint; the
# partitioner in `outo_models_cli.api.lfs` uses this value.
_MAX_FILE_BYTES = 100 * 1024 * 1024

# 50 MiB total upload cap before the CLI auto-batches per-file. Anything
# smaller goes up in one multipart request.
_SINGLE_REQUEST_TOTAL_BYTES = 50 * 1024 * 1024

_VALID_KINDS = ("model", "dataset", "space")


# ---------------------------------------------------------------------------
# Consoles
# ---------------------------------------------------------------------------

console = Console(stderr=False)
err_console = Console(stderr=True)


def _errprint(message: str) -> None:
    """Print a single-line message to stderr."""
    err_console.print(f"[bold red]error[/bold red]: {message}")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_kind(raw: str) -> str:
    """Return `raw` if it's a known repo kind, else raise `OmcError`."""
    if raw not in _VALID_KINDS:
        valid = ", ".join(_VALID_KINDS)
        raise OmcError(f"Invalid kind {raw!r}. Use one of: {valid}.")
    return raw


def _visibility(public: bool, private: bool) -> str:
    """Resolve the two-flag visibility pair into a single keyword."""
    if public and private:
        raise OmcError("Pass only one of --public / --private.")
    if public:
        return "public"
    if private:
        return "private"
    return "private"  # default matches the server's default


def _parse_owner_repo(raw: str) -> tuple[str, str]:
    """Split `owner/name` into `(owner, name)`; raise on malformed input."""
    if "/" not in raw or raw.count("/") > 1:
        raise OmcError("Argument must be in the form `<owner>/<name>`.")
    owner, name = raw.split("/", 1)
    if not owner or not name:
        raise OmcError(
            "Argument must be in the form `<owner>/<name>` (both parts required).",
        )
    return owner, name


# ---------------------------------------------------------------------------
# Server resolution
# ---------------------------------------------------------------------------


def _resolve_target(store: Store, *, server: str | None) -> tuple[str, httpx.Client]:
    """Return `(base_url, httpx.Client)` for the requested target.

    The CLI uses a short-lived client per command so a failed auth does
    not poison subsequent calls. Token resolution happens inside
    `store.resolve` (env var → stored entry).
    """
    base_url, token = store.resolve(server)
    return base_url, api.with_client(base_url, token)


def resolve_username(
    store: Store,
    *,
    base_url: str,
    token: str,
    config_path: Path | None,
) -> tuple[str, Store]:
    """Return the username for `base_url`, fetching via `/api/auth/me` if missing.

    LFS endpoints and the `/resolve/...` raw-file endpoint both require
    HTTP Basic auth (`username:token`), so the CLI needs the username in
    addition to the PAT. Login stores it (`ServerEntry.username`), but
    older config files and the `OMC_TOKEN` env-var path do not carry one
    — those flows call `me()` to learn the username and the helper
    persists the result when there is a backing config file.
    """
    entry = store.get(base_url)
    if entry is not None and entry.username:
        return entry.username, store
    with api.with_client(base_url, token) as client:
        identity = api.me(client)
    new_store = store.with_username(base_url, identity.username)
    if entry is not None and config_path is not None:
        config.save_store(new_store, config_path)
    return identity.username, new_store


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _collect_files(path: Path) -> tuple[list[Path], Path]:
    """Return `(file_paths, file_root)` from a user-supplied file or folder."""
    if not path.exists():
        raise FileMissingError(f"Local path does not exist: {path}")
    if path.is_file():
        return [path], path.parent
    if path.is_dir():
        files = [p for p in path.rglob("*") if p.is_file()]
        if not files:
            raise OmcError(f"No files found under {path}.")
        return files, path
    raise FileMissingError(f"Not a file or directory: {path}")


def _check_size_limit(files: Iterable[Path]) -> None:
    """Raise `FileTooLargeError` if any single file exceeds the per-file cap."""
    for fpath in files:
        if fpath.stat().st_size > _MAX_FILE_BYTES:
            size_mib = fpath.stat().st_size / (1024 * 1024)
            raise FileTooLargeError(
                f"{fpath} is {size_mib:.1f} MiB. Files over 100 MiB must be uploaded "
                "via git + LFS. See `git lfs track` and the server's LFS guide.",
            )


def _human_bytes(num: int) -> str:
    """Render byte counts in the operator-facing summary line."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(num)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


__all__ = [
    "_MAX_FILE_BYTES",
    "_SINGLE_REQUEST_TOTAL_BYTES",
    "_VALID_KINDS",
    "_check_size_limit",
    "_collect_files",
    "_errprint",
    "_human_bytes",
    "_parse_owner_repo",
    "_resolve_target",
    "_validate_kind",
    "_visibility",
    "console",
    "err_console",
    "resolve_username",
]

# `os` is used implicitly through `os.environ` callers in `commands.auth`,
# but the import here documents the dependency for readers / linters.
_ = os
