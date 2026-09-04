"""Host kernel tuning for rootless low-port binding.

outo-models runs rootless containers as uid 1000. The kernel only permits
unprivileged binds to TCP ports >= `net.ipv4.ip_unprivileged_port_start`
(default 1024), so serving on 80/443 needs the threshold lowered on the
host. The flow mirrors the firewall layer (AGENTS.md §2.3): the wizard calls
`ensure_low_ports()`; on a host shell it runs the bundled script (which
self-elevates via sudo), and inside a container it raises
`OutoError(low_ports_host_required)` carrying the exact host command.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from outo_models.exceptions import OutoError
from outo_models.firewall.open_ports import in_container

_SYSCTL_PATH = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")

_SCRIPT_ENV_VAR = "OUTO_LOW_PORTS_SCRIPT"

_DEFAULT_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "scripts" / "enable-low-ports.sh"
)

# Exact host command the wizard prints when it cannot apply the setting
# itself (container). Tests assert on this byte-for-byte.
HOST_LOW_PORTS_COMMAND = "/usr/local/share/outo-models/enable-low-ports.sh <min-port>"


@dataclass(frozen=True, slots=True)
class LowPortsResult:
    """Outcome of an ensure call: threshold before/after, or planned argv."""

    min_port: int
    was_blocked: bool
    commands: list[list[str]]


def unprivileged_port_start(sysctl_path: Path = _SYSCTL_PATH) -> int | None:
    """Current kernel threshold, or None when unreadable (non-Linux, restricted)."""
    try:
        return int(sysctl_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def low_ports_blocked(min_port: int, *, sysctl_path: Path = _SYSCTL_PATH) -> bool:
    """True when the kernel would refuse an unprivileged bind to `min_port`."""
    current = unprivileged_port_start(sysctl_path)
    if current is None:
        return False
    return current > min_port


def _resolve_script_path() -> str:
    override = os.environ.get(_SCRIPT_ENV_VAR)
    if override:
        return override
    return str(_DEFAULT_SCRIPT_PATH)


async def ensure_low_ports(min_port: int, *, dry_run: bool = False) -> LowPortsResult:
    """Ensure unprivileged binds to `min_port` are allowed on the host.

    Raises:
        OutoError(low_ports_host_required): running inside a container — the
            message carries `HOST_LOW_PORTS_COMMAND` verbatim for the wizard
            to print.
        OutoError(low_ports_command_failed): the host script exited non-zero.
    """
    if not low_ports_blocked(min_port):
        return LowPortsResult(min_port=min_port, was_blocked=False, commands=[])

    if in_container():
        raise OutoError(
            (
                "unprivileged low-port binding must be enabled on the host; "
                "this process is inside a container. Run the following command "
                "on the host (the script will prompt for sudo itself when "
                f"needed): {HOST_LOW_PORTS_COMMAND}"
            ),
            code="low_ports_host_required",
        )

    argv = ["bash", _resolve_script_path(), str(min_port)]
    if dry_run:
        return LowPortsResult(min_port=min_port, was_blocked=True, commands=[argv])

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_bytes.decode(errors="replace").strip()
        raise OutoError(
            f"low-ports script failed (exit={proc.returncode}): {stderr or argv}",
            code="low_ports_command_failed",
        )
    return LowPortsResult(min_port=min_port, was_blocked=True, commands=[argv])
