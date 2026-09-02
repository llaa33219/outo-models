"""Network helpers — IP / interface probes used by the setup wizard.

Two surface helpers:

* `is_ip_address(value)` — cheap, pure-function classifier. Returns True iff
  `value` parses as an IPv4 or IPv6 address (loopback included). The point is
  to keep the "internal mode vs. hostname" decision out of the wizard's
  control flow so the same predicate can be reused by `Settings.is_internal`
  and the security-headers middleware.

* `detect_lan_ipv4()` — best-effort detection of the host's outbound IPv4
  address. The probe is a `socket.connect()` to `192.0.2.1:80` — TEST-NET-1,
  reserved for documentation per RFC 5737 — so no packets actually leave
  the interface. The kernel just populates the socket's local endpoint with
  the address it WOULD route from, which is the same value `ip route get`
  would surface. Any error (no network, no IPv4 default route, no
  permissions) collapses to `None`; callers fall back to manual entry.

Why UDP for a probe that never sends? `socket(AF_INET, SOCK_DGRAM)` is the
canonical trick — `connect()` on a UDP socket records the local endpoint
without touching the wire. The actual byte is never written.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from typing import Final

# Test-net-1 (RFC 5737) — guaranteed unrouted, safe to use as a probe target.
_LAN_PROBE_HOST: Final[str] = "192.0.2.1"
_LAN_PROBE_PORT: Final[int] = 80


def is_ip_address(value: str) -> bool:
    """Return True iff `value` parses as an IPv4 or IPv6 address.

    `ipaddress.ip_address()` accepts both `ip_address` forms; hostnames,
    empty strings, and whitespace-only input all return False. The check is
    deliberately tight — anything that includes a dot-or-colon but isn't a
    real address (e.g. `"not_an_ip"`) must not be misclassified as an IP.
    """
    if not value:
        return False
    candidate = value.strip()
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def detect_lan_ipv4() -> str | None:
    """Return the host's outbound IPv4 address, or None on any failure.

    Implementation:

    1. Open a UDP socket against `192.0.2.1:80`. The kernel routes the
       outbound interface WITHOUT sending anything (UDP `connect` only
       binds the local endpoint).
    2. Read `getsockname()` to capture the address the kernel chose.
    3. Close the socket and return the captured address.

    The function never raises. `OSError` (no network), `socket.gaierror`
    (DNS), and a missing IPv4 default route all collapse to `None` so the
    caller can prompt the operator instead.
    """
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((_LAN_PROBE_HOST, _LAN_PROBE_PORT))
        local = sock.getsockname()
        if not local or not local[0]:
            return None
        address = str(local[0])
        # Sanity: must round-trip through `ipaddress` so the caller can
        # trust the result without re-validating. `is_ip_address` is the
        # canonical gate the rest of the codebase uses.
        if not is_ip_address(address):
            return None
        return address
    except OSError:
        return None
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


__all__ = ["detect_lan_ipv4", "is_ip_address"]
