"""`outo-models reset` — destructive cleanup, gated by the triple-confirm rule.

AGENTS.md §2.2 — the triple-yes gate is inviolable. This module is the
*only* code path allowed to delete container / volume / local data, and
it implements the rule exactly as the spec demands:

    * Default (no `--destroy`) → DRY RUN. Print what would be destroyed
      (user count, repo count, total bytes, volume name) and exit 0 with
      a reminder to re-run with `--destroy`.
    * `--destroy` requires `OUTO_DESTRUCTIVE=1` in the environment. Without
      it → refusal message, exit 1.
    * `OUTO_DESTRUCTIVE=1` without `--destroy` → still DRY RUN.
    * Both present → three prompts in succession, each printing an
      escalating summary. The literal answer `yes` (no trailing whitespace,
      no caps, no Korean, no Y/N shortcut) is the ONLY string that counts;
      anything else aborts with exit 1.
    * On a non-interactive stdin (EOF before any prompt completes) the
      command must abort safely with exit 1 — never default to "yes".
    * Only after all three are entered exactly does the command:
        1. Run `assets/scripts/reset.sh` (host-side container wipe).
        2. Wipe the local `data_dir` (dev installs without podman).

The dry-run summary is computed against the local DB (the operator runs
this on the server host); an empty database simply prints zeros, which
is the expected behavior for a fresh install that has been "reset".
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console
from sqlalchemy import func, select

from outo_models.cli import (
    container_script,
    format_bytes,
    podman_script_env,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.config import get_settings
from outo_models.db import Repo, User, get_engine, get_session_factory
from outo_models.exceptions import ConfigError, OutoError

# Env var the triple-yes gate requires. Same convention as `OUTO_CONFIG`
# etc. Documented in `docs/cli.md` (operator-facing) and `docs/security.md`
# (rationale).
_DESTRUCTIVE_ENV = "OUTO_DESTRUCTIVE"

# The literal token that counts as "yes" — anything else aborts.
_YES_TOKEN = "yes"  # noqa: S105 — keyword, not a password

# How many times the operator must type exactly `yes` for the gate to open.
# Bumping this number requires changing `AGENTS.md §2.2` first; tests assert
# on this exact value so an accidental change fails CI immediately.
_REQUIRED_YES_COUNT = 3

# Container / volume names match `assets/scripts/reset.sh`. They are
# duplicated here only so the dry-run summary can render the planned
# destruction without spawning the script.
_CONTAINER_NAME = "outo-models"
_VOLUME_NAME = "outo-models-data"


def reset(
    destroy: bool = typer.Option(
        False,
        "--destroy",
        help="Perform the actual deletion (must pass the confirmation gate). Dry-run by default.",
    ),
) -> None:
    """`outo-models reset` — wipe all data (triple-confirmation gate).

    The default action is a dry run: it prints a summary of what would be
    deleted and exits 0. To perform the actual deletion you must pass
    `--destroy` together with the environment variable `OUTO_DESTRUCTIVE=1`,
    and type exactly `yes` three times.
    """
    _reset_impl(destroy=destroy)


async def _compute_summary() -> tuple[int | None, int | None, int | None, str]:
    """Return `(user_count, repo_count, total_bytes, volume_name)`.

    Counts are `None` when the data cannot actually be measured — the DB
    file missing (fresh install, or the destroy path through the host shim
    where the volume is deliberately NOT mounted so the CLI container never
    holds what it deletes). Showing fabricated zeros for a destructive gate
    would understate the destruction, so None renders as "all" instead.
    """
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    repos_dir = data_dir / "repos"

    user_count: int | None = None
    repo_count: int | None = None
    total_bytes: int | None = None

    db_file = data_dir / "db.sqlite3"
    if db_file.is_file():
        try:
            engine = get_engine(settings)
            factory = get_session_factory(engine)
            async with factory() as session:
                user_count = (
                    await session.execute(select(func.count()).select_from(User))
                ).scalar_one()
                repo_count = (
                    await session.execute(select(func.count()).select_from(Repo))
                ).scalar_one()
        except Exception:
            user_count = None
            repo_count = None

    if repos_dir.exists():
        total = 0
        for path in repos_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        total_bytes = total

    return user_count, repo_count, total_bytes, _VOLUME_NAME


def _print_dry_run(
    user_count: int | None, repo_count: int | None, total_bytes: int | None, volume: str
) -> None:
    """Print the would-be-destroyed summary."""

    def _c(value: int | None) -> str:
        return str(value) if value is not None else "unknown (not measurable here)"

    def _b(value: int | None) -> str:
        return format_bytes(value) if value is not None else "unknown"

    console = Console()
    console.print(
        "[bold yellow]\\[dry-run] The following data would be deleted "
        "(no actual deletion will happen):[/bold yellow]"
    )
    console.print(f"  - users: {_c(user_count)}")
    console.print(f"  - repositories: {_c(repo_count)}")
    console.print(f"  - disk usage: {_b(total_bytes)}")
    console.print(f"  - container: {_CONTAINER_NAME}")
    console.print(f"  - volume: {volume}")
    config_path = Path(os.environ.get("OUTO_CONFIG", "/etc/outo-models/config.yaml"))
    console.print(f"  - config files: {config_path.parent} (config.yaml, Caddyfile, …)")
    console.print()
    console.print(
        "To actually delete, pass the [bold]--destroy[/bold] option "
        f"together with the environment variable [bold]{_DESTRUCTIVE_ENV}=1[/bold]."
    )


_IRREVERSIBLE_WARNING = (
    "[WARNING] This action is irreversible (no recovery). All data will be gone for good."
)


def _print_escalation_warning(
    stage: int, user_count: int | None, repo_count: int | None, total_bytes: int | None
) -> None:
    """Print the escalating warning shown above each `yes` prompt."""
    console = Console(stderr=True)
    if user_count is None:
        # Counts unknown (volume not mounted / fresh install): name WHAT is
        # destroyed without inventing numbers — a wrong "0 users" would
        # understate a destructive action.
        summaries = {
            1: "[Are you sure?] This will permanently delete ALL server data.",
            2: _IRREVERSIBLE_WARNING,
            3: (
                f"[FINAL CONFIRMATION] The container '{_CONTAINER_NAME}', the volume "
                f"'{_VOLUME_NAME}' with all its contents, the local data directory, "
                "and the generated config files will all be deleted."
            ),
        }
    else:
        summaries = {
            1: (
                f"[Are you sure?] This will permanently delete {user_count} users, "
                f"{repo_count} repositories, {format_bytes(total_bytes or 0)} of data."
            ),
            2: _IRREVERSIBLE_WARNING,
            3: (
                f"[FINAL CONFIRMATION] The container '{_CONTAINER_NAME}' and the volume "
                f"'{_VOLUME_NAME}', plus the local data directory, will all be deleted. "
                f"{user_count} users and {repo_count} repositories will disappear."
            ),
        }
    console.print(f"\n[bold red]{summaries[stage]}[/bold red]")


def _gather_yes_confirmations(
    user_count: int | None, repo_count: int | None, total_bytes: int | None
) -> bool:
    """Run the triple-yes gate; return True iff every prompt accepted `yes`.

    A non-interactive stdin (EOF) aborts safely — the builtin `input()`
    raises `EOFError` in that case, which we catch and translate into a
    structured refusal (no default-to-yes surprise).
    """
    console = Console(stderr=True)
    for stage in range(1, _REQUIRED_YES_COUNT + 1):
        _print_escalation_warning(stage, user_count, repo_count, total_bytes)
        # `input()` (not `rich.prompt.Confirm`) so the answer must be
        # exactly `yes` — `Confirm.ask` would silently accept `y` and
        # weaken the AGENTS.md §2.2 gate.
        try:
            answer = input(f"[{stage}/{_REQUIRED_YES_COUNT}] type '{_YES_TOKEN}' exactly: ")
        except EOFError:
            console.print("[bold red]Input stream closed. Aborting.[/bold red]")
            return False
        # `answer != _YES_TOKEN` — no `.strip()`, so `yes ` (trailing
        # whitespace) is rejected. The spec demands exact match; an
        # operator who truly meant `yes` types it without trailing space.
        if answer != _YES_TOKEN:
            console.print(f"[bold red]Not '{_YES_TOKEN}' — aborting.[/bold red]")
            return False
    return True


def _wipe_local_data_dir() -> None:
    """Remove `data_dir` from a dev install (no podman on this host).

    This is a separate step from the container wipe because dev installs
    have a `data_dir` under pytest's tmp_path (or the operator's checkout),
    not inside a podman volume.
    """
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    if not data_dir.exists():
        return
    try:
        shutil.rmtree(data_dir)
    except OSError as exc:
        raise OutoError(
            f"failed to delete local data directory ({data_dir}): {exc}",
            code="reset_local_wipe_failed",
        ) from exc


def _wipe_config_dir() -> None:
    """Remove the wizard-generated config files (config.yaml, Caddyfile).

    Reset must return the machine to the first-install state: without this,
    the next `setup` would silently reuse the previous domain/secrets. The
    directory itself is kept — the host shim bind-mounts it and podman
    refuses missing bind sources.
    """
    config_path = Path(os.environ.get("OUTO_CONFIG", "/etc/outo-models/config.yaml"))
    config_dir = config_path.parent
    if not config_dir.is_dir():
        return
    for entry in config_dir.iterdir():
        if entry.name == "config.example.yaml":
            continue  # shipped example, not operator state
        try:
            entry.unlink() if entry.is_file() or entry.is_symlink() else shutil.rmtree(entry)
        except OSError as exc:
            raise OutoError(
                f"failed to delete {entry}: {exc}",
                code="reset_config_wipe_failed",
            ) from exc


def _reset_impl(destroy: bool) -> None:
    """Top-level handler — split out so tests can call it directly."""
    summary = asyncio.run(_compute_summary())
    user_count, repo_count, total_bytes, volume = summary

    env_destructive = os.environ.get(_DESTRUCTIVE_ENV) == "1"

    if not destroy:
        _print_dry_run(user_count, repo_count, total_bytes, volume)
        if env_destructive:
            note = (
                f"\n[yellow]Note: {_DESTRUCTIVE_ENV}=1 is set but "
                "--destroy was not given, so this is a dry run.[/yellow]"
            )
            Console().print(note)
        asyncio.run(_dispose_engines_safe())
        raise typer_exit(0)

    if not env_destructive:
        asyncio.run(_dispose_engines_safe())
        render_error(
            ConfigError(
                f"--destroy requires the environment variable {_DESTRUCTIVE_ENV}=1.",
                code="reset_env_missing",
            )
        )
        raise typer_exit(1)

    if not _gather_yes_confirmations(user_count, repo_count, total_bytes):
        asyncio.run(_dispose_engines_safe())
        render_error(OutoError("confirmation gate failed.", code="reset_aborted"))
        raise typer_exit(1)

    script = container_script("reset.sh")
    rc = stream_subprocess(["bash", script], env=podman_script_env())
    if rc != 0:
        asyncio.run(_dispose_engines_safe())
        render_error(OutoError(f"reset.sh failed (exit={rc})", code="reset_script_failed"))
        raise typer_exit(1)

    try:
        _wipe_local_data_dir()
        _wipe_config_dir()
    except OutoError as exc:
        asyncio.run(_dispose_engines_safe())
        render_error(exc)
        raise typer_exit(1) from exc

    Console().print(
        "[bold green][done] outo-models has been reset to a freshly-installed state.[/bold green]"
    )
    Console().print("Run `outo-models setup` to start over.")


async def _dispose_engines_safe() -> None:
    """Best-effort engine teardown for the reset path.

    `_compute_summary` opens the engine in its own `asyncio.run()` cycle,
    so the aiosqlite worker threads belong to a closed loop when
    `_reset_impl` returns. `dispose_engines()` against those threads
    raises `RuntimeError: Event loop is closed`; we swallow that because
    reset is read-only against the DB — the next command will rebuild a
    fresh engine.
    """
    import contextlib

    from outo_models.db.engine import dispose_engines

    engine = get_engine()
    with contextlib.suppress(Exception):
        await engine.dispose()
    with contextlib.suppress(Exception):
        await dispose_engines()


__all__ = ["reset"]
