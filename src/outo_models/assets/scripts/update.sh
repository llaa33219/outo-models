#!/usr/bin/env bash
# update.sh — outo-models image refresh + DB migration + restart
#
# Invoked on the host by `outo-models update` (the CLI knows this script's
# path). Behavior:
#   1. pull the image tag from argument 1 (default outo-models:stable)
#   2. run `outo-models migrate` in a throwaway container (DB migration)
#   3. `"${podman_cmd[@]}" restart` if a container with the same name is running
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
#   otherwise migration/pull/restart failure code

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
# The `migrate` subcommand is provided by the CLI. If `"${podman_cmd[@]}" run` fails with
# "unknown command", set -e surfaces a non-zero code — that is a legitimate
# signal and the fix is to update the image, not to patch this script.
echo "[2/3] running DB migration"
"${podman_cmd[@]}" run --rm \
    -v "${volume_name}:/var/lib/outo-models" \
    "${image_tag}" \
    outo-models migrate

# -----------------------------------------------------------------------------
# 3) restart the existing container (only if present)
# -----------------------------------------------------------------------------
echo "[3/3] checking container restart"
if "${podman_cmd[@]}" container exists "${container_name}"; then
    "${podman_cmd[@]}" restart "${container_name}"
    echo "      restarted container ${container_name}."
else
    echo "      no running ${container_name} container. Start it manually:"
    echo "        "${podman_cmd[@]}" run -d --name ${container_name} -p 80:80 -p 443:443 \\"
    echo "          -v ${volume_name}:/var/lib/outo-models \\"
    echo "          --cap-add NET_BIND_SERVICE \\"
    echo "          ${image_tag}"
fi

cat <<'EOF'
[done] outo-models update finished.
EOF
