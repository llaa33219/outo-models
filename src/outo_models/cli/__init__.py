"""Shared helpers for the `outo-models` CLI.

The Typer commands live in sibling modules (`setup`, `start`, `reset`, ...);
this package's `__init__.py` owns the *cross-command* helpers every command
imports. Concentrating them here keeps each command module focused on its
single subcommand and avoids a private helper module that the test ownership
list did not budget for.

Three concerns live here:

1. **`prompts` — Rich Prompt facade with a swappable backend.**
   Tests monkeypatch `outo_models.cli.prompts.text` / `.password` /
   `.confirm` / `.int_prompt` to feed canned answers into CliRunner cases.
   Production calls go through `rich.prompt.Prompt` so the wizard works
   without a tty (a documented limitation, not a regression — the
   non-interactive wizard path is the recommended automation surface).

2. **`render_error()` — single funnel for typed CLI failures.**
   `OutoError` carries a stable `.code` (machine-readable) and a
   human-readable message (operator-facing). The Typer callback prints
   the message in red and exits with code 1 — never a Python traceback,
   since `AGENTS.md §2.1` forbids leaking secrets and tracebacks routinely do.

3. **`run_capture()` — buffered `subprocess.run` for commands we must read.**

   The twin of `stream_subprocess()`: same env-merge semantics, but stdout
   and stderr come back as text instead of reaching the operator's terminal
   unbuffered. Used where the *content* of a failure is the diagnosis input
   (`podman run` refusing a low port) — inherited stderr would leave nothing
   left to match against.

4. **`container_script()` — locate bundled `assets/scripts/*.sh`.**
   Mirrors `firewall.open_ports._resolve_script_path` and
   `tls.caddy_manager._resolve_template_path` (both honor an `OUTO_*_SCRIPT`
   env override). The CLI uses it for `update.sh` / `reset.sh`; bundling
   the lookup here keeps the wizard / reset command symmetrical with the
   existing firewall helper.

Nothing here is re-exported from the sibling subcommand modules — these
are internal building blocks, deliberately behind a `cli.` import.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from typer import Exit as _TyperExit

from outo_models.exceptions import OutoError
from outo_models.utils import humanbytes as _humanbytes

# ---------------------------------------------------------------------------
# Prompts (testable)
# ---------------------------------------------------------------------------


class Prompts:
    """Rich-backed prompt facade with a swappable test surface.

    Every method accepts only keyword arguments (so positional mistakes
    are caught at the call site), and returns a *plain* Python value —
    `str`, `bool`, `int` — never a prompt-toolkit object. CliRunner tests
    swap individual methods to feed canned answers.

    The wrapper is intentionally narrow: anything richer than text /
    password / confirm / int_prompt is better expressed with a custom
    `rich.prompt.Prompt` subclass *inside* the wizard, not in this
    generic layer.
    """

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool] | None = None,
    ) -> str:
        """Prompt for a free-form string.

        Args:
            message: Question printed before the input cursor.
            default: Returned when the user just presses Enter.
            validate: Optional predicate; if it returns False the prompt
                re-asks. The CLI never uses this for security gates — it
                surfaces a re-prompt, never an error.
        """
        while True:
            value = Prompt.ask(message, default=default)
            if validate is None or validate(value):
                return value

    def password(
        self,
        message: str,
        *,
        validate: Callable[[str], bool] | None = None,
    ) -> str:
        """Prompt for a password (input hidden in the terminal)."""
        while True:
            value = Prompt.ask(message, password=True)
            if validate is None or validate(value):
                return value

    def confirm(self, message: str, *, default: bool = False) -> bool:
        """Prompt for a yes/no answer; never raises on EOF."""
        return bool(Confirm.ask(message, default=default))

    def choice(
        self,
        message: str,
        *,
        choices: list[str],
        default: str = "",
    ) -> str:
        """Prompt the operator to pick one of `choices`.

        Accepts the choice value verbatim (`stable`) or its 1-based
        number in the rendered list. Re-prompts silently until valid —
        the wizard re-asks for *any* unrecognised value rather than
        surfacing a typed error (the same UX as `text` with a
        `validate=` predicate).

        `default` is returned when the operator just presses Enter.
        """
        valid = set(choices)
        rendered = "\n".join(f"  [{i + 1}] {choice}" for i, choice in enumerate(choices))
        prompt = f"{message}\n{rendered}"
        while True:
            value = Prompt.ask(prompt, default=default)
            if value in valid:
                return value
            try:
                idx = int(value) - 1
            except ValueError:
                continue
            if 0 <= idx < len(choices):
                return choices[idx]

    def int_prompt(
        self,
        message: str,
        *,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Prompt for an integer with optional inclusive bounds.

        Re-prompts on bad input rather than raising — the wizard's UX is
        "ask until you get something usable", not "raise ValidationFailed
        and exit".
        """
        default_str = str(default) if default is not None else ""
        while True:
            raw = Prompt.ask(message, default=default_str)
            try:
                value = int(raw)
            except ValueError:
                continue
            if minimum is not None and value < minimum:
                continue
            if maximum is not None and value > maximum:
                continue
            return value


# Module-level singleton — monkeypatch this in tests (e.g.
# `monkeypatch.setattr(cli_mod.prompts, "text", lambda *a, **k: "x")`).
prompts = Prompts()


# ---------------------------------------------------------------------------
# Error rendering
# ---------------------------------------------------------------------------

# Exit codes: 1 for operational failure, 0 for success. We intentionally
# do not adopt `sysexits.h` codes (64/EX_USAGE etc.) because every CLI
# command's failure mode is "the operation did not complete"; mapping them
# to a single code keeps the contract simple for scripts that wrap us.
_EXIT_OK = 0
_EXIT_FAIL = 1


def render_error(exc: BaseException) -> None:
    """Print a clean, single-line error to stderr.

    The function is called from both the Typer callback (for `OutoError`)
    and any command that wants to swallow a known failure. Unknown
    exceptions are re-raised so the test harness catches them.
    """
    if isinstance(exc, OutoError):
        message = str(exc) or exc.__class__.__name__
        Console(stderr=True).print(f"[bold red]error[/bold red] ({exc.code}): {message}")
        return
    raise exc


def typer_exit(code: int = _EXIT_FAIL) -> _TyperExit:
    """Build a `typer.Exit` with the project's default failure code.

    Imported at module load time so the function call stays cheap; the
    `typer` dependency is required by the CLI anyway, and tests that do
    not use Typer simply do not import this helper.
    """
    return _TyperExit(code=code)


# ---------------------------------------------------------------------------
# Host-side scripts (update.sh / reset.sh)
# ---------------------------------------------------------------------------

# Map script name → env-var override. Same convention as
# `firewall.open_ports._SCRIPT_ENV_VAR` and
# `tls.caddy_manager._TEMPLATE_ENV_VAR`.
_CONTAINER_SCRIPT_ENV: dict[str, str] = {
    "update.sh": "OUTO_UPDATE_SCRIPT",
    "reset.sh": "OUTO_RESET_SCRIPT",
}


def container_script(name: str) -> str:
    """Return the absolute path to a bundled host-side script.

    Honors `OUTO_UPDATE_SCRIPT` / `OUTO_RESET_SCRIPT` env overrides for
    operators who vendor the script outside the wheel. The scripts ship as
    package data under `outo_models/assets/scripts/` so they exist in the
    installed wheel (a repo-relative path would not).
    """
    env_var = _CONTAINER_SCRIPT_ENV.get(name)
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return override
    return str(Path(__file__).resolve().parents[1] / "assets" / "scripts" / name)


def stream_subprocess(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Run `argv` with stdout/stderr inherited.

    Used for the host-side `update.sh` / `reset.sh` scripts: the operator
    watches the output stream, so redirecting it through a `Console` would
    only add buffering. `check=False` means we propagate the return code
    without raising — the wizard decides how to surface failures. `env`, when
    given, is merged over os.environ (used to hand PODMAN_BIN/PODMAN_URL to
    the scripts when we run through podman-remote).
    """
    merged = None if env is None else {**os.environ, **env}
    result = subprocess.run(argv, check=False, shell=False, env=merged)  # noqa: S603 — argv is fixed
    return int(result.returncode)


def run_capture(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run `argv` with both streams captured; return `(exit_code, output)`.

    The buffered twin of `stream_subprocess()`. Use it when the *text* a
    command failed with is itself the diagnosis input (see `cli.start`): with
    inherited stderr the message is gone once the child exits, and there is
    nothing left to match a failure signature against. `output` is
    `stdout + stderr`, so a message on either stream is matched either way.
    `env`, when given, is merged over `os.environ` with the same semantics as
    `stream_subprocess()`.
    """
    merged = None if env is None else {**os.environ, **env}
    result = subprocess.run(  # noqa: S603 — argv is fixed
        argv,
        check=False,
        shell=False,
        env=merged,
        capture_output=True,
        text=True,
    )
    return int(result.returncode), (result.stdout or "") + (result.stderr or "")


def podman_base() -> list[str]:
    """Base argv for a podman invocation, or [] when podman is unreachable.

    Two modes:
      1. `podman` on PATH (host shell) → ["podman"].
      2. Only the API socket is available — the CLI shim mounts the host's
         socket into the container and sets OUTO_PODMAN_SOCKET — and the
         image ships `podman-remote` → ["podman-remote", "--url", "unix://…"].
    """
    if shutil.which("podman"):
        return ["podman"]
    remote = shutil.which("podman-remote")
    if not remote:
        return []
    sock = os.environ.get("OUTO_PODMAN_SOCKET", "/run/podman/podman.sock")
    if Path(sock).is_socket():
        return [remote, "--url", f"unix://{sock}"]
    return []


def podman_available() -> bool:
    """Return True iff podman is reachable (binary or remote socket)."""
    return bool(podman_base())


def podman_script_env() -> dict[str, str]:
    """Env for the bash glue scripts so they use the same podman channel."""
    base = podman_base()
    if not base:
        return {}
    env = {"PODMAN_BIN": base[0]}
    if len(base) > 1:
        env["PODMAN_URL"] = base[-1]
    return env


def print_status(message: str) -> None:
    """Print a single line to stdout — used for post-step status notes."""
    Console().print(message)


# ---------------------------------------------------------------------------
# Byte size formatting + parsing
# ---------------------------------------------------------------------------


def format_bytes(num_bytes: int) -> str:
    """Render `num_bytes` as a human-readable string for CLI output.

    Used by the `reset` dry-run summary. We deliberately do NOT use this
    for the quota CLI's *input* (which accepts `10GiB` strings instead) so
    the input and output vocabularies stay distinct.
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(num_bytes)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


# The parser itself lives in `outo_models.utils.humanbytes` so routers can
# reuse it without importing `rich` / `typer`. Re-exported here because
# `outo_models.cli.parse_human_bytes` is part of the documented CLI surface.
parse_human_bytes = _humanbytes.parse_human_bytes


__all__ = [
    "Prompts",
    "container_script",
    "format_bytes",
    "parse_human_bytes",
    "podman_available",
    "print_status",
    "prompts",
    "render_error",
    "run_capture",
    "stream_subprocess",
    "typer_exit",
]
