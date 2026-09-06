"""Typer entry point for the `omc` console script.

This module owns the root `app`, the `--version` / `--debug` callbacks,
and the central error funnel every command routes through. Commands
live in sibling modules; `main.py` only assembles them.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from outo_models_cli import __version__, config
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
    help="Command-line client for self-hosted outo-models servers.",
    no_args_is_help=True,
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
    """`omc` root callback — wires shared flags and the credential store."""
    config_path = config.config_path()
    state = _load_state(config_path, debug=debug)
    ctx.obj = {
        "store": config.load_store(config_path),
        "config_path": config_path,
        "debug": debug,
        "state": state,
    }


# Subcommand registrations.
app.add_typer(auth_app, name="auth", help="Manage stored credentials.")
app.add_typer(repo_app, name="repo", help="Manage repositories.")
app.command("ls", help="List a directory of a repository.")(ls_command)
app.command("download", help="Recursively download a repository.")(download_command)
app.command("upload", help="Upload a file or directory to a repository.")(upload_command)


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
