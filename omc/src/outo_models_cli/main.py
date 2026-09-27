"""Typer entry point for the `omc` console script.

This module owns the root `app`, the `--version` / `--debug` callbacks,
and the central error funnel every command routes through. Commands
live in sibling modules; `main.py` only assembles them.

The root app's help is intentionally long: it is the page an AI agent
or a new user lands on when they type `omc` or `omc --help`. Every
section (PURPOSE, CONCEPTS, GETTING STARTED, COMMAND INDEX, CONFIG,
MORE HELP) is sourced from `commands/_help_text` so the help text and
the test markers stay in lock-step.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from outo_models_cli import __version__, config
from outo_models_cli.commands._help_text import (
    AUTH_APP_HELP,
    DOWNLOAD_EPILOG,
    LS_EPILOG,
    REPO_APP_HELP,
    ROOT_HELP,
    UPLOAD_EPILOG,
)
from outo_models_cli.commands.auth import auth_app
from outo_models_cli.commands.download import download_command
from outo_models_cli.commands.ls import ls_command
from outo_models_cli.commands.repo import repo_app
from outo_models_cli.commands.upload import upload_command
from outo_models_cli.errors import OmcError

# ---------------------------------------------------------------------------
# Per-invocation state — passed through `ctx.obj`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppState:
    """State propagated through the Typer context.

    `config_path` is the resolved file path (defaulted from
    `XDG_CONFIG_HOME` / `$HOME/.config`). `debug` is the `--debug` flag;
    when true, the error funnel re-raises so a Python traceback is
    printed instead of the single English line.
    """

    config_path: Path
    debug: bool


def _load_state(config_path: Path, debug: bool) -> AppState:
    """Build the per-invocation state — split out so tests can call it."""
    return AppState(config_path=config_path, debug=debug)


# ---------------------------------------------------------------------------
# Root Typer app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="omc",
    help=ROOT_HELP,
    invoke_without_command=True,
    rich_markup_mode="rich",
    add_completion=False,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when `--version` is passed."""
    if value:
        typer.echo(f"omc {__version__}")
        raise typer.Exit(code=0)


@app.callback()
def _root_callback(
    ctx: typer.Context,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the package version and exit.",
        ),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Show full Python tracebacks on error (default: single English line).",
        ),
    ] = False,
) -> None:
    """`omc` root callback — wires shared flags and the credential store.

    When no subcommand is supplied (bare `omc`), the help is printed
    and the process exits 0. Without this branch Click would treat the
    missing-subcommand case as a usage error (exit 2) because the
    callback signature has arguments; the explicit branch converts it
    into the documented "show help and exit" behaviour.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    config_path = config.config_path()
    state = _load_state(config_path, debug=debug)
    ctx.obj = {
        "store": config.load_store(config_path),
        "config_path": config_path,
        "debug": debug,
        "state": state,
    }


# Subcommand registrations. The long-form help text lives on the
# sub-Typer itself (AUTH_APP_HELP / REPO_APP_HELP); passing the same
# constant to `add_typer` ensures `omc auth --help` / `omc repo --help`
# render the full structured description, not just the first line.
# `short_help` is the one-liner the parent Commands table uses; the
# multi-line `help` would otherwise leak "PURPOSE" into the table.
app.add_typer(
    auth_app,
    name="auth",
    help=AUTH_APP_HELP,
    short_help="Manage stored credentials.",
)
app.add_typer(
    repo_app,
    name="repo",
    help=REPO_APP_HELP,
    short_help="Manage repositories.",
)
# `short_help` is the one-liner shown in the parent's Commands table;
# the full PURPOSE / USAGE description is sourced from each command's
# docstring (Typer picks multi-line docstrings up automatically).
app.command(
    "ls",
    short_help="List a directory of a repository.",
    epilog=LS_EPILOG,
)(ls_command)
app.command(
    "download",
    short_help="Recursively download a repository.",
    epilog=DOWNLOAD_EPILOG,
)(download_command)
app.command(
    "upload",
    short_help="Upload a file or directory to a repository.",
    epilog=UPLOAD_EPILOG,
)(upload_command)


# ---------------------------------------------------------------------------
# Central error funnel — `typer.Exit(1)` on every OmcError
# ---------------------------------------------------------------------------


def _render_error(exc: BaseException) -> None:
    """Print the canonical English error line and exit 1.

    Mirrors the server's `render_error` helper (`outo_models.cli`) so a
    shell wrapper can grep for `error: ...` regardless of which CLI it
    invokes. Tracebacks are only shown when `--debug` is set, which
    keeps the user's shell history free of stack frames for routine
    401s and 404s.
    """
    from outo_models_cli.commands._shared import _errprint

    if isinstance(exc, OmcError):
        _errprint(str(exc))
        return
    raise exc


def cli() -> None:
    """Console-script entry point — wraps `app()` in a single error funnel."""
    try:
        app()
    except OmcError as exc:
        _render_error(exc)
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception as exc:
        debug = bool(getattr(app, "_debug", False))
        if debug:
            raise
        _render_error(OmcError(f"Unexpected error: {exc}"))
        raise SystemExit(1) from exc


def cli_main() -> None:
    """`python -m outo_models_cli` entry point — same funnel, module form."""
    cli()


if __name__ == "__main__":
    cli_main()


__all__ = ["app", "cli", "cli_main"]


# Anchor the stdlib import for ruff/linters that otherwise flag unused imports.
_ = sys
