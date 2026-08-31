"""Host-side firewall port-opening orchestrator.

The container itself runs unprivileged (see AGENTS.md §2.3) and must NOT
touch `firewall-cmd` / `ufw` / `nft` directly. Instead, the setup wizard calls
`open_ports()` from inside the container, which builds a precise argv for the
bundled `container/scripts/firewall-open.sh` and executes it on the host.

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

# `src/outo_models/firewall/open_ports.py` is 3 parents deep from the repo root.
# parents[3] lands on the directory that contains `container/`.
_DEFAULT_SCRIPT_RELATIVE = Path("container") / "scripts" / "firewall-open.sh"


@dataclass(frozen=True, slots=True)
class OpenPortsResult:
    """Outcome of a port-opening attempt — the wizard / UI displays this verbatim.

    Attributes:
        kind: The firewall backend that was actually targeted (after detection).
        opened: Ports that were opened (or attempted). Empty in dry_run.
        commands: Exact argv for every invocation. In dry_run this is the
            planned argv; otherwise it records what was executed (including
            the `sudo -n` prefix when the process is not uid 0).
        needs_sudo: True iff the argv was prefixed with `sudo -n`.
    """

    kind: FirewallKind
    opened: list[int]
    commands: list[list[str]]
    needs_sudo: bool


def _resolve_script_path() -> str:
    """Resolve the bundled firewall script, honoring the env-var override."""
    override = os.environ.get(_SCRIPT_ENV_VAR)
    if override:
        return override
    # `Path(__file__).parents[3]` walks up from
    # `src/outo_models/firewall/open_ports.py` to the repo root.
    repo_root = Path(__file__).resolve().parents[3]
    return str(repo_root / _DEFAULT_SCRIPT_RELATIVE)


def _build_argv(script: str, kind: FirewallKind, ports: list[int]) -> tuple[list[str], bool]:
    """Build the host-side argv and report whether `sudo -n` must prefix it.

    The container is unprivileged; we delegate to `sudo -n` (non-interactive)
    so a password prompt cannot hang the wizard. Operators who want
    interactive sudo can run the wizard under sudo themselves.
    """
    base = ["bash", script, kind.value, *(str(p) for p in ports)]
    if os.geteuid() == 0:
        return base, False
    return ["sudo", "-n", *base], True


async def _run_argv(argv: list[str]) -> None:
    """Spawn `argv`, raise `OutoError(firewall_permission)` on sudo failure."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        # `sudo -n` returns 1 with "a password is required" when NOPASSWD is
        # not configured; treat that distinctly so the wizard can tell the
        # operator exactly what to do.
        if argv[0] == "sudo":
            raise OutoError(
                "firewall commands require elevated privileges; re-run the wizard under sudo "
                "or grant NOPASSWD to the invoking user via /etc/sudoers.d/outo-models",
                code="firewall_permission",
            )
        stderr = stderr_bytes.decode(errors="replace").strip()
        raise OutoError(
            f"firewall script failed (exit={proc.returncode}): {stderr or argv}",
            code="firewall_command_failed",
        )


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
        OpenPortsResult with `kind`, `opened`, `commands`, and `needs_sudo`.

    Raises:
        OutoError(firewall_permission): `sudo -n` failed (NOPASSWD missing).
        OutoError(firewall_command_failed): the host script returned non-zero.
    """
    port_list = [int(p) for p in ports]
    resolved_kind = kind if kind is not None else await detect_firewall()
    script = _resolve_script_path()
    argv, needs_sudo = _build_argv(script, resolved_kind, port_list)

    if dry_run:
        return OpenPortsResult(
            kind=resolved_kind,
            opened=[],
            commands=[list(argv)],
            needs_sudo=needs_sudo,
        )

    await _run_argv(argv)
    return OpenPortsResult(
        kind=resolved_kind,
        opened=port_list,
        commands=[list(argv)],
        needs_sudo=needs_sudo,
    )
