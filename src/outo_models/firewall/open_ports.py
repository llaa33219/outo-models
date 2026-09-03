"""Host-side firewall port-opening orchestrator.

The container itself runs unprivileged (see AGENTS.md §2.3) and must NOT
touch `firewall-cmd` / `ufw` / `nft` directly. Instead, the setup wizard calls
`open_ports()` from inside the container, which builds a precise argv for the
bundled `assets/scripts/firewall-open.sh` and executes it on the host.

Elevation is the host script's responsibility, not ours: when the script is
invoked without root it `exec sudo bash "$0" "$@"`s itself so an interactive
password prompt is allowed. The Python side therefore never spawns `sudo`,
never inspects `geteuid`, and never refuses to run because of missing
privileges — if the host has sudo available, the script will prompt; if not,
the script exits with a clear English error.

When invoked from inside a container, opening host firewall ports is
physically impossible (the container shares the host network namespace only
when run with `--network=host`, and even then it cannot talk to
`firewall-cmd` / `ufw` / `nft`). In that case `open_ports()` raises
`OutoError("firewall_container_host_required")` whose message contains the
exact host command the operator must run on the host. The setup wizard
catches that code and prints the command verbatim, so the wizard can complete
inside the container while still telling the operator what to do.

Why a bash script and not a Python wrapper?
    * the script is short, declarative, and inspectable by the operator
      during `outo-models reset` / post-mortem
    * it lives next to the Caddyfile + systemd units in `container/scripts/`,
      the one place a sysadmin expects host-side glue to live
    * every command is its own argv array — `shell=True` is never used
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from outo_models.exceptions import OutoError
from outo_models.firewall.detect import FirewallKind, detect_firewall

# Ports the server MUST be reachable on for the Caddy reverse-proxy to handle
# HTTP/01 ACME challenges and HTTPS traffic.
REQUIRED_PORTS: tuple[int, ...] = (80, 443)

# Env var that overrides the package-relative script lookup. Used by tests
# and by operators who vendor the script outside the wheel.
_SCRIPT_ENV_VAR = "OUTO_FIREWALL_SCRIPT"

# The script ships as package data under `outo_models/assets/` so it is
# present in the installed wheel too (a repo-relative path would not be).
_DEFAULT_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "scripts" / "firewall-open.sh"
)

# Standard container marker files. The first is created by Docker; the second
# by Podman / generic OCI runtimes. We probe both because the orchestrator
# must behave the same regardless of which engine launched the container.
_MARKER_PATHS: tuple[Path, ...] = (
    Path("/.dockerenv"),
    Path("/run/.containerenv"),
)

# Exact host command the wizard must tell operators to run on the host.
# Lives at the module level so (a) tests can assert on it byte-for-byte and
# (b) the wizard (cli/setup/_effect.py) can splice it into its guidance.
# `auto` = the script detects the host firewall kind itself (the container
# cannot probe it for us).
HOST_FIREWALL_COMMAND = "/usr/local/share/outo-models/firewall-open.sh auto <ports...>"


@dataclass(frozen=True, slots=True)
class OpenPortsResult:
    """Outcome of a port-opening attempt — the wizard / UI displays this verbatim.

    Attributes:
        kind: The firewall backend that was actually targeted (after detection).
        opened: Ports that were opened (or attempted). Empty in dry_run.
        commands: Exact argv for every invocation. The host script self-elevates
            via `sudo`, so the argv never contains `sudo` on the Python side.
    """

    kind: FirewallKind
    opened: list[int]
    commands: list[list[str]]


def _in_container() -> bool:
    """True iff the current process is running inside a container.

    Probes the standard Docker / Podman / OCI marker files. Cheap to call:
    two `stat`s on absolute paths the kernel resolves instantly.
    """
    return any(p.exists() for p in _MARKER_PATHS)


def _resolve_script_path() -> str:
    """Resolve the bundled firewall script, honoring the env-var override."""
    override = os.environ.get(_SCRIPT_ENV_VAR)
    if override:
        return override
    return str(_DEFAULT_SCRIPT_PATH)


def _build_argv(script: str, kind: FirewallKind, ports: list[int]) -> list[str]:
    """Build the argv the orchestrator will spawn.

    The script self-elevates (see `firewall-open.sh`); the Python side never
    prefixes `sudo` and never inspects `geteuid`. This keeps the orchestrator
    honest — one code path, one argv shape — and lets interactive sudo prompt
    the operator instead of failing on `sudo -n`.
    """
    return ["bash", script, kind.value, *(str(p) for p in ports)]


def _container_error(ports: list[int]) -> OutoError:
    """Build the typed error that tells the operator exactly what to run on the host.

    The message MUST contain `HOST_FIREWALL_COMMAND` verbatim — the setup
    wizard prints that placeholder command so the operator knows where the
    host-side script lives and that it will prompt for sudo itself. The
    command uses the `auto` kind: the host script detects the firewall
    backend itself, which the container cannot do on the host's behalf.
    """
    return OutoError(
        (
            "firewall port-opening cannot run inside a container; the host "
            "firewall must be opened on the host. Run the following command "
            "on the host (the script will prompt for sudo itself when "
            f"needed): {HOST_FIREWALL_COMMAND}"
        ),
        code="firewall_container_host_required",
    )


async def _run_argv(argv: list[str]) -> None:
    """Spawn `argv`, raise `OutoError(firewall_command_failed)` on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        # Include the stderr tail verbatim so the wizard can surface the
        # exact reason to the operator (missing tool, bad config, etc.).
        stderr = stderr_bytes.decode(errors="replace").strip()
        message = f"firewall script failed (exit={proc.returncode}): {stderr or argv}"
        raise OutoError(message, code="firewall_command_failed")


async def open_ports(
    ports: Iterable[int] = REQUIRED_PORTS,
    kind: FirewallKind | None = None,
    dry_run: bool = False,
) -> OpenPortsResult:
    """Open `ports`/tcp on the host firewall, then return a structured result.

    Args:
        ports: Iterable of TCP port numbers. Generators and sets are accepted;
            order is preserved so the recorded argv reads in insertion order.
        kind: Backend to target. `None` triggers `detect_firewall()`.
        dry_run: If True, plan the argv but DO NOT spawn the script.

    Returns:
        OpenPortsResult with `kind`, `opened`, `commands`.

    Raises:
        OutoError(firewall_container_host_required): the orchestrator is
            running inside a container; the message contains the exact host
            command the operator must run themselves.
        OutoError(firewall_command_failed): the host script returned non-zero;
            the message carries the script's stderr tail.
    """
    port_list = [int(p) for p in ports]

    if _in_container():
        # Check BEFORE detection: the container image ships no firewall
        # tooling, so detect_firewall() here is meaningless (and must never
        # be allowed to crash the wizard). The host command names the `auto`
        # kind — the script detects the backend on the host itself.
        raise _container_error(port_list)

    resolved_kind = kind if kind is not None else await detect_firewall()
    script = _resolve_script_path()
    argv = _build_argv(script, resolved_kind, port_list)

    if dry_run:
        return OpenPortsResult(
            kind=resolved_kind,
            opened=[],
            commands=[list(argv)],
        )

    await _run_argv(argv)
    return OpenPortsResult(
        kind=resolved_kind,
        opened=port_list,
        commands=[list(argv)],
    )
