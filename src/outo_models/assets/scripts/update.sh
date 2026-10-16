#!/usr/bin/env bash
# update.sh — outo-models image refresh + DB migration + container recreate
#
# Invoked on the host by `outo-models update` (the CLI knows this script's
# path). Behavior:
#   1. pull the image tag from argument 1 (default outo-models:stable)
#   2. run `outo-models server migrate` in a throwaway container (DB migration)
#   3. run `outo-models start` from the NEW image (recreate the container with
#      the current spec)
#
# Step 3 recreates instead of restarting: `podman restart` reuses the OLD
# container spec, so spec changes shipped with the new image (mounts, env,
# ports) never applied — the field failure where an update-only upgrade left
# Spaces container SDKs reporting "Cannot connect to the Podman socket" and
# left the operator needing a manual `outo-models start`. `start` replaces an
# existing container with the current spec, so the recreate IS `start`.
#
# On hosts without podman this exits 0 — this script must only ever be
# invoked on the host, so it should never run inside a container. It stays
# graceful so environments like CI don't break.
#
# Usage:
#   update.sh [<image-tag>]
#
# Exit codes:
#   0   success (or no-op due to missing podman)
#   64  usage error
#   otherwise pull/migration/recreate failure code

set -euo pipefail

# The CLI hands us the podman channel when it runs through the shim
# (podman-remote over the mounted host socket); on the host plain
# `podman` is used. PODMAN_URL is only set for the remote case.
podman_cmd=("${PODMAN_BIN:-podman}")
if [[ -n "${PODMAN_URL:-}" ]]; then
    podman_cmd+=(--url "${PODMAN_URL}")
fi

if [[ $# -gt 1 ]]; then
    echo "usage: $0 [<image-tag>]" >&2
    exit 64
fi

image_tag="${1:-outo-models:stable}"
container_name="outo-models"
volume_name="outo-models-data"

# -----------------------------------------------------------------------------
# podman presence check — graceful exit 0 when absent
# -----------------------------------------------------------------------------
if ! command -v "${podman_cmd[0]}" >/dev/null 2>&1; then
    cat <<'EOF'
[note] podman is not installed on this host.

  update.sh is a host-side script, so it does not need to do anything
  outside a container deployment. You can ignore this message.

  If this IS a container deployment host, install podman and re-run.
EOF
    exit 0
fi

# -----------------------------------------------------------------------------
# 1) pull the new image
# -----------------------------------------------------------------------------
echo "[1/3] pulling image: ${image_tag}"
"${podman_cmd[@]}" pull "${image_tag}"

# -----------------------------------------------------------------------------
# 2) migration (throwaway container, same data volume mounted)
# -----------------------------------------------------------------------------
# The image entrypoint already prepends `outo-models` — the argv here is the
# subcommand path only (`server migrate`), NOT `outo-models server migrate`
# (which produced "No such command 'outo-models'" in the field).
# --userns=keep-id keeps migration-written DB files owned by the invoking
# host user, consistent with the server container (mixed subuid ownership
# breaks the DB); /etc/outo-models is mounted so migrate reads config.yaml.
echo "[2/3] running DB migration"
"${podman_cmd[@]}" run --rm \
    --userns=keep-id \
    -v /etc/outo-models:/etc/outo-models:ro \
    -v "${volume_name}:/var/lib/outo-models" \
    "${image_tag}" \
    server migrate

# -----------------------------------------------------------------------------
# 3) recreate the server container with `start` from the NEW image
# -----------------------------------------------------------------------------
# The NEW image supplies both the CLI code and the target spec: the container
# that launched this script may still carry the previous release's CLI.
#
# The invocation mirrors the host CLI shim (scripts/install-cli.sh) so `start`
# sees exactly what it sees when an operator types `outo-models start`:
#   --network=host    the health probe `start` runs reaches 127.0.0.1 / the domain
#   --userns=keep-id  same rootless UID as the shim (podman API + file ownership)
#   /etc/outo-models  the config.yaml `start` reads (image, volume, ports)
#   the data volume   same data_dir the server container runs with
#   the host socket   `start` drives the host engine through it AND mounts the
#                     same socket into the new server container
#
# Two env vars, two directions: OUTO_PODMAN_SOCKET is the in-container path
# `start` connects through, OUTO_PODMAN_SOCK is the HOST path it mounts into
# the server container. Passing both is what makes an update-only upgrade pick
# up the socket mount (a restart kept the pre-mount spec forever).
host_sock="${OUTO_PODMAN_SOCK:-}"
# A stale host shim (pre-v0.6.6) mounts the host socket at
# /run/podman/podman.sock but forwards no HOST path, so the probe below would
# read that container-side mount point and pass it to podman as a `-v` SOURCE
# — the host engine then dies with "statfs ...: permission denied". Inside a
# container only a forwarded OUTO_PODMAN_SOCK is a valid bind source.
if [[ -z "${host_sock}" ]] \
    && [[ -f /.dockerenv || -f /run/.containerenv || -n "${container:-}" ]]; then
    cat >&2 <<'EOF'
[error] cannot recreate the server container: the host CLI shim did not
forward a Podman socket path (OUTO_PODMAN_SOCK). The in-container
/run/podman/podman.sock mount point is not a valid host bind source, so the
recreate is aborted instead of crashing with "permission denied".

  Refresh the host CLI shim, then re-run the update:

    curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash
    outo-models update

  The image pull and the DB migration above already completed, and your
  running server container is untouched — it keeps serving traffic.
EOF
    exit 1
fi

if [[ -z "${host_sock}" ]]; then
    user_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
    if [[ -S "${user_sock}" ]]; then
        host_sock="${user_sock}"
    elif [[ -S /run/podman/podman.sock ]]; then
        host_sock="/run/podman/podman.sock"
    fi
fi

recreate_args=(
    --network=host
    --userns=keep-id
    -v /etc/outo-models:/etc/outo-models
    -v "${volume_name}:/var/lib/outo-models"
)
if [[ -n "${host_sock}" ]]; then
    recreate_args+=(
        -v "${host_sock}:/run/podman/podman.sock"
        -e OUTO_PODMAN_SOCKET=/run/podman/podman.sock
        -e "OUTO_PODMAN_SOCK=${host_sock}"
    )
fi

echo "[3/3] recreating container ${container_name} with the ${image_tag} spec"
"${podman_cmd[@]}" run --rm \
    "${recreate_args[@]}" \
    "${image_tag}" \
    start

cat <<'EOF'
[done] outo-models update finished.
EOF
