"""`outo-models` — the operator's single CLI entry point.

Every subcommand lives in a sibling module (`setup`, `serve`, `migrate`,
`start`, `stop`, `restart`, `status`, `update`, `reset`, `admin`). This
module owns three things only:

    1. The Typer application object (`app`) — the console_script
       `outo-models = "outo_models.cli.main:app"` reads.
    2. The `--version` flag (printed from `outo_models.version`).
    3. The error funnel — every `OutoError` raised by any subcommand is
       rendered as a single human-readable line + exit 1, never a
       traceback.

Why one fat Typer callback instead of per-command exception handlers?
    * The CLI's safety contract ("no tracebacks leak secrets") is a single
      property, easier to audit at one site than across a dozen handlers.
    * Typer 0.27's callback-decorated sub-app pattern still requires
      every command to opt in, and forgetting one command means a leaked
      traceback. One site enforces it for every command by construction.
"""

from __future__ import annotations

import typer

from outo_models import version
from outo_models.cli import render_error
from outo_models.cli.admin import admin_app
from outo_models.cli.reset import reset
from outo_models.cli.server import server_app
from outo_models.cli.setup import setup_app
from outo_models.cli.update import update
from outo_models.exceptions import OutoError

# The top-level Typer app. The help text is operator-visible English.
app = typer.Typer(
    name="outo-models",
    help="Operator CLI for a self-hosted outo-models server.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when `--version` is passed."""
    if value:
        typer.echo(f"outo-models {version.__version__}")
        raise typer.Exit(code=0)


@app.callback()
def _root_callback(
    version_flag: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the package version and exit.",
    ),
) -> None:
    """`outo-models` operator CLI root callback.

    `OutoError` is rendered as a single human-readable line + exit 1;
    Python tracebacks are never printed (AGENTS.md §2.1).
    """


app.add_typer(setup_app, name="setup", help="First-run interactive setup wizard")
app.add_typer(server_app, name="server", help="In-container server / migration commands")

# Lifecycle commands are top-level Typer commands (each a single leaf
# action) so we avoid a redundant sub-app for one command. Imports are
# placed here (not at the top) to keep the import graph free of cycles —
# `start` etc. do not import the parent `app`.
from outo_models.cli.restart import restart  # noqa: E402
from outo_models.cli.start import start  # noqa: E402
from outo_models.cli.status import status  # noqa: E402
from outo_models.cli.stop import stop  # noqa: E402

app.command("start", help="Start the outo-models container.")(start)
app.command("stop", help="Stop the outo-models container.")(stop)
app.command("restart", help="Restart the outo-models container.")(restart)
app.command("status", help="Show the outo-models container status.")(status)

app.command("update", help="Pull the new image, run DB migrations, and restart.")(update)
app.command("reset", help="Wipe the container and all data (triple-yes gate).")(reset)
app.add_typer(admin_app, name="admin", help="Manage users, quotas, and GPUs")


def main() -> None:
    """Console-script entry point.

    Wraps `app()` in a try/except that funnels `OutoError` into the
    project's standard renderer, so neither the operator nor a CI script
    ever sees a Python traceback for a known failure mode.
    """
    try:
        app()
    except OutoError as exc:
        render_error(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
