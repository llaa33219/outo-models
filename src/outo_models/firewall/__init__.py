"""Host firewall detection + port-opening orchestration.

Public API (consumed by the setup wizard in WP-14):

    from outo_models.firewall import (
        FirewallKind,      # StrEnum: FIREWALLD | UFW | NFTABLES | NONE
        detect_firewall,   # async: probe the host for the active backend
        is_port_open,      # async: True / False / None per backend
        open_ports,        # async: orchestrate the host script (with dry_run)
        OpenPortsResult,   # dataclass: structured outcome
        REQUIRED_PORTS,    # (80, 443)
    )

The container runs unprivileged (AGENTS.md §2.3); `open_ports` builds an argv
for `container/scripts/firewall-open.sh` and never shells out via `shell=True`.
"""

from outo_models.firewall.detect import (
    FirewallKind,
    detect_firewall,
    is_port_open,
)
from outo_models.firewall.open_ports import (
    REQUIRED_PORTS,
    OpenPortsResult,
    open_ports,
)

__all__ = [
    "REQUIRED_PORTS",
    "FirewallKind",
    "OpenPortsResult",
    "detect_firewall",
    "is_port_open",
    "open_ports",
]
