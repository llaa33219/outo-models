"""Firewall backend detection.

The container runs unprivileged (see AGENTS.md §2.3), so it cannot talk to
firewalld / ufw / nftables directly. Instead, the CLI invokes small host-side
probes from inside the container and decides which backend is in charge before
asking the bundled `assets/scripts/firewall-open.sh` to mutate firewall state.

Probing order is fixed: firewalld → ufw → nftables → NONE. A binary that is
missing (FileNotFoundError, exit 127) or returns an unexpected result simply
skips that backend — only a positive match ends the search.
"""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum

# `firewall-cmd --state` prints exactly "running" or "not running\n"; match the
# whole first line so "not running" does not collide with "running".
_FIREWALLD_RUNNING_RE = re.compile(r"^running\s*$", re.MULTILINE)


class FirewallKind(StrEnum):
    """Identified firewall backend. String values double as the argv passed to the
    host-side script, so they MUST stay lowercase + snake-case free."""

    FIREWALLD = "firewalld"
    UFW = "ufw"
    NFTABLES = "nftables"
    NONE = "none"


async def _run(*args: str) -> tuple[int, str]:
    """Run `args` via asyncio subprocess, return `(returncode, stdout)`.

    A missing binary (exit 127) or any non-zero exit is propagated to the
    caller as a `(non-zero, "")` tuple — callers decide whether a non-zero
    means "not this backend" or a hard failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        # Binary not installed — report as "not this backend" (127) instead
        # of crashing the caller. Seen in the field: the container image has
        # no firewall tools, so detection inside it must not explode.
        return 127, ""
    stdout_bytes, _ = await proc.communicate()
    # `communicate()` returns the process's exit code; `None` would mean the
    # process is still running, which the asyncio contract forbids here.
    rc = proc.returncode if proc.returncode is not None else 0
    return rc, stdout_bytes.decode(errors="replace")


async def detect_firewall() -> FirewallKind:
    """Identify the active firewall backend, in priority order.

    Returns:
        FirewallKind: one of FIREWALLD / UFW / NFTABLES / NONE.

    Detection rules (each applied in order; first hit wins):
        - firewalld: `firewall-cmd --state` exits 0 AND stdout contains "running".
        - ufw:       `ufw status`         exits 0 AND stdout contains "Status: active".
        - nftables:  `nft --version`      exits 0 AND `nft list ruleset` exits 0.
    """
    rc, out = await _run("firewall-cmd", "--state")
    if rc == 0 and _FIREWALLD_RUNNING_RE.search(out) is not None:
        return FirewallKind.FIREWALLD

    rc, out = await _run("ufw", "status")
    if rc == 0 and "Status: active" in out:
        return FirewallKind.UFW

    rc, _ = await _run("nft", "--version")
    if rc == 0:
        rc, _ = await _run("nft", "list", "ruleset")
        if rc == 0:
            return FirewallKind.NFTABLES

    return FirewallKind.NONE


async def is_port_open(port: int, kind: FirewallKind) -> bool | None:
    """Check whether `port`/tcp is currently open according to `kind`.

    Returns:
        True  : the backend reports the port open.
        False : the backend reports the port closed.
        None  : the backend is missing, unusable, or cannot answer.

    The contract is explicit: `None` is the honest answer when the host does
    not have the necessary tools installed. Callers should treat `None` and
    `False` differently — `None` means "ask the operator", `False` means
    "go ahead and open it".
    """
    if kind == FirewallKind.NONE:
        return None

    if kind == FirewallKind.FIREWALLD:
        rc, _ = await _run("firewall-cmd", f"--query-port={port}/tcp")
        if rc == 0:
            return True
        if rc == 1:
            return False
        # 127 / other: binary missing or unexpected error.
        return None

    if kind == FirewallKind.UFW:
        rc, out = await _run("ufw", "status")
        if rc != 0 or "Status: active" not in out:
            return None
        # `ufw status` rows look like: `443/tcp   ALLOW IN   Anywhere`.
        # Match the port column; the action column decides True/False.
        pattern = re.compile(rf"^{port}/tcp\s+(\S+)", re.MULTILINE)
        match = pattern.search(out)
        if match is None:
            return None
        action = match.group(1).upper()
        return action.startswith("ALLOW")

    # NFTABLES
    rc, out = await _run("nft", "list", "ruleset")
    if rc != 0:
        return None
    # A rule accepting this dport appears as `tcp dport <port> ... accept`.
    # Match on a word boundary so 80 does not accidentally match 8080.
    pattern = re.compile(rf"tcp\s+dport\s+{port}(?:\D|$)")
    return pattern.search(out) is not None
