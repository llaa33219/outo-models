#!/usr/bin/env bash
# outo-models firewall port-opening script
#
# The CLI invokes this on the host side as a non-root user. When it runs
# unprivileged it `exec sudo bash "$0" "$@"`s itself so an interactive sudo
# prompt can appear — `sudo -n` would deadlock the wizard. The Python
# orchestrator never prefixes `sudo` and never inspects `geteuid`; elevation
# lives entirely in this script. After elevation the script is as
# idempotent as possible — invoking it multiple times with the same
# arguments never errors.
#
# Usage:
#   firewall-open.sh <kind> <port...>
#
# kind:
#   firewalld  : firewalld permanent rules + reload
#   ufw        : ufw allow
#   nftables   : add dport rules to a dedicated outo_models table/chain
#   none       : no firewall detected — print guidance and exit 0
#
# Exit codes:
#   0  : success (or no change)
#   1  : sudo not installed (cannot self-elevate)
#   64 : usage error (EX_USAGE)
#   otherwise : tool execution failure

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <kind> <port...>" >&2
    exit 64
fi

kind=$1
shift

if [[ $# -eq 0 ]]; then
    echo "at least one port required" >&2
    exit 64
fi

# Arguments arrive as trusted argv, but defensively re-validate integers only.
for p in "$@"; do
    if ! [[ $p =~ ^[0-9]+$ ]] || (( p < 1 || p > 65535 )); then
        echo "invalid port: ${p}" >&2
        exit 64
    fi
done

# Self-elevation: the script is shipped as a world-readable file at
# /usr/local/share/outo-models/firewall-open.sh. The container invokes it
# directly (not via sudo), so on a non-root invocation we re-exec ourselves
# under sudo. The exec replaces this process — there is no return path, so
# the rest of the script only ever runs as root. An interactive password
# prompt is allowed; the wizard waits for it.
if [[ "$(id -u)" != "0" ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "[error] sudo is not installed; re-run this script as root, or install sudo." >&2
        exit 1
    fi
    exec sudo bash "$0" "$@"
fi

add_firewalld() {
    local p
    for p in "$@"; do
        firewall-cmd --permanent --add-port="${p}/tcp"
    done
    firewall-cmd --reload
}

add_ufw() {
    local p
    for p in "$@"; do
        ufw allow "${p}/tcp"
    done
}

add_nftables() {
    # Create a dedicated table/chain and accumulate rules there. Skip rules
    # that already exist.
    nft add table inet outo_models 2>/dev/null || true
    nft add chain inet outo_models outo_models_input \
        '{ type filter hook input priority 0 ; policy accept ; }' 2>/dev/null || true
    for p in "$@"; do
        if nft list ruleset | grep -qE "tcp dport ${p}([[:space:]]|$)"; then
            echo "nftables: port ${p} already open"
        else
            nft add rule inet outo_models outo_models_input tcp dport "${p}" counter accept
        fi
    done
}

case "$kind" in
    firewalld)
        add_firewalld "$@"
        ;;
    ufw)
        add_ufw "$@"
        ;;
    nftables)
        add_nftables "$@"
        ;;
    none)
        cat <<'EOF'
No firewall was detected. You must open the externally reachable ports
(80, 443) yourself in the OS firewall or your cloud security group.
EOF
        ;;
    *)
        echo "unknown firewall kind: ${kind}" >&2
        exit 64
        ;;
esac
