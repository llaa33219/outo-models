#!/usr/bin/env bash
# enable-low-ports.sh — allow unprivileged processes to bind low TCP ports.
#
# outo-models runs its containers rootless as uid 1000. The kernel only
# permits unprivileged binds to ports >= net.ipv4.ip_unprivileged_port_start
# (default 1024), so serving on 80/443 needs the threshold lowered on the
# HOST. This script writes a persistent sysctl drop-in and applies it.
#
# Elevation: invoked without root it `exec sudo bash "$0" "$@"`s itself so an
# interactive sudo prompt can appear (same convention as firewall-open.sh).
#
# Usage:
#   enable-low-ports.sh [<min-port>]        # default: 80
#
# Exit codes:
#   0  : threshold already low enough, or applied successfully
#   1  : sudo unavailable / sysctl apply failed
#   64 : usage error (EX_USAGE)

set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "usage: $0 [<min-port>]" >&2
    exit 64
fi

min_port="${1:-80}"
if ! [[ $min_port =~ ^[0-9]+$ ]] || (( min_port < 0 || min_port > 1024 )); then
    echo "invalid min-port: ${min_port} (expected 0..1024)" >&2
    exit 64
fi

current=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo "1024")
if (( current <= min_port )); then
    echo "[ok] unprivileged low-port binding already allowed (threshold ${current} <= ${min_port})"
    exit 0
fi

# Self-elevate after the cheap check so a no-op never prompts for sudo.
if [[ "$(id -u)" != "0" ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "[error] sudo is not installed; re-run this script as root, or install sudo." >&2
        exit 1
    fi
    exec sudo bash "$0" "$@"
fi

drop_in="/etc/sysctl.d/90-outo-models-low-ports.conf"
printf 'net.ipv4.ip_unprivileged_port_start=%s\n' "${min_port}" > "${drop_in}"
sysctl --system >/dev/null

new_value=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)
if (( new_value > min_port )); then
    echo "[error] sysctl apply failed: threshold is still ${new_value} (> ${min_port})" >&2
    exit 1
fi

echo "[done] unprivileged low-port binding enabled (threshold ${new_value}, persistent via ${drop_in})"
