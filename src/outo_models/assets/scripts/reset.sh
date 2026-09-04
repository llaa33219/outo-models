#!/usr/bin/env bash
# reset.sh — delete the outo-models container and its data volume
#
# **All safety gates are the caller's responsibility.** This script performs
# no confirmation whatsoever — it assumes the caller already passed the
# "three yes" gate from AGENTS.md §2 ("reset safety mechanism is immutable").
#
# Behavior:
#   1. stop + rm the outo-models container if it exists (ignore otherwise, idempotent)
#   2. rm the outo-models-data volume if it exists (ignore otherwise, idempotent)
#   3. print a completion message
#
# On hosts without podman this exits non-zero — unlike update.sh, this script
# performs a highly destructive action (data deletion), so a missing podman
# on what claims to be a deployment host is a configuration error the caller
# must hear about.
#
# Usage: reset.sh (no arguments)
# Exit codes: 0 (success / idempotent no-op)
#             1 (missing podman, or an actual deletion failure — usually permissions)
#             64 (usage error)

set -euo pipefail

# The CLI hands us the podman channel when it runs through the shim
# (podman-remote over the mounted host socket); on the host plain
# `podman` is used. PODMAN_URL is only set for the remote case.
podman_cmd=("${PODMAN_BIN:-podman}")
if [[ -n "${PODMAN_URL:-}" ]]; then
    podman_cmd+=(--url "${PODMAN_URL}")
fi

if [[ $# -ne 0 ]]; then
    echo "usage: $0  (no arguments)" >&2
    exit 64
fi

container_name="outo-models"
volume_name="outo-models-data"

# -----------------------------------------------------------------------------
# podman presence check
# -----------------------------------------------------------------------------
if ! command -v "${podman_cmd[0]}" >/dev/null 2>&1; then
    cat >&2 <<'EOF'
[error] podman is not installed on this host.

  reset.sh performs a highly destructive action: deleting the container and
  its data volume. Without podman nothing was deleted, and the caller must
  be told that clearly.

  If this is a container deployment host, install podman and re-run.
EOF
    exit 1
fi

# -----------------------------------------------------------------------------
# 1) stop + remove the container (only if present)
# -----------------------------------------------------------------------------
if "${podman_cmd[@]}" container exists "${container_name}"; then
    echo "[1/3] stopping container: ${container_name}"
    # A failed stop (e.g. already stopped) must not abort — idempotency is the goal.
    "${podman_cmd[@]}" stop "${container_name}" >/dev/null 2>&1 || true
    echo "[2/3] removing container: ${container_name}"
    "${podman_cmd[@]}" rm "${container_name}"
else
    echo "[1/3] no ${container_name} container — skipping."
    echo "[2/3] (skipped) nothing to remove."
fi

# -----------------------------------------------------------------------------
# 2) remove any OTHER containers still holding the volume
# -----------------------------------------------------------------------------
# Leaked throwaway CLI containers (killed before --rm could clean them up)
# and stale migrate runs keep the volume busy and make `volume rm` fail
# with "volume is being used". A reset is only complete when the volume is
# actually gone, so remove every holder, not just the named container.
if "${podman_cmd[@]}" volume exists "${volume_name}"; then
    # Never sweep the container we are running in — removing ourselves
    # mid-script is a self-kill (seen twice in the field). Self ids are
    # harvested from three sources because each has a failure mode:
    # hostname (wrong under --network=host), /proc/self/cgroup (no id under
    # rootless cgroup v2), /run/.containerenv (podman >= 4.x only).
    self_patterns=("$(hostname)")
    cgroup_file="${OUTO_SELF_CGROUP_FILE:-/proc/self/cgroup}"
    if [[ -r "${cgroup_file}" ]]; then
        while IFS= read -r cid; do
            [[ -n "${cid}" ]] && self_patterns+=("${cid}")
        done < <(grep -oE '[0-9a-f]{64}' "${cgroup_file}" || true)
    fi
    containerenv_file="${OUTO_SELF_CONTAINERENV_FILE:-/run/.containerenv}"
    if [[ -r "${containerenv_file}" ]]; then
        while IFS= read -r cid; do
            [[ -n "${cid}" ]] && self_patterns+=("${cid}")
        done < <(grep -oE '[0-9a-f]{64}' "${containerenv_file}" || true)
    fi

    mapfile -t raw_holders < <("${podman_cmd[@]}" ps -a --filter "volume=${volume_name}" -q)
    holders=()
    for candidate in "${raw_holders[@]}"; do
        [[ -n "${candidate}" ]] || continue
        skip=0
        for pat in "${self_patterns[@]}"; do
            [[ -n "${pat}" ]] || continue
            # Self-match in either direction (short id vs full id prefix).
            if [[ "${candidate}" == "${pat}" || "${candidate}" == "${pat}"* || "${pat}" == "${candidate}"* ]]; then
                skip=1
                break
            fi
        done
        (( skip )) && continue
        holders+=("${candidate}")
    done

    for holder in "${holders[@]}"; do
        echo "      removing volume holder: ${holder}"
        "${podman_cmd[@]}" stop "${holder}" >/dev/null 2>&1 || true
        "${podman_cmd[@]}" rm "${holder}"
    done
fi

# -----------------------------------------------------------------------------
# 3) remove the data volume (only if present)
# -----------------------------------------------------------------------------
if "${podman_cmd[@]}" volume exists "${volume_name}"; then
    echo "[3/3] removing data volume: ${volume_name}"
    "${podman_cmd[@]}" volume rm "${volume_name}"
    echo "      all git repositories, the SQLite DB, and Caddy certificate state are gone."
else
    echo "[3/3] no ${volume_name} volume — skipping."
fi

cat <<'EOF'
[done] outo-models has been returned to its first-install state.
       The next run must start from the setup wizard again:
         outo-models setup
EOF
