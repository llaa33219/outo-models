#!/usr/bin/env bash
# outo-models container entrypoint.
#
# Responsibilities (and nothing more):
#   1. Print an English start banner with the version
#   2. AGENTS.md §4: reject the IMAGE_FLAVOR=dev + OUTO_ENV=production combination
#   3. Warn ahead of time if non-root cannot bind 80/443
#   4. Notify if /etc/outo-models/config.yaml is missing (not required)
#   5. `exec outo-models "$@"` — replace the shell so signals reach the
#      CLI directly (SIGTERM/SIGINT are forwarded to uvicorn immediately).
#
# Environment variables:
#   IMAGE_FLAVOR  injected as a build ARG (Containerfile ARG IMAGE_FLAVOR).
#                 Only stable | dev are valid; any other value is rejected
#                 at build time.
#   OUTO_ENV      Mirrors Settings.env. development | production.
#   OUTO_DATA_DIR Data directory (default /var/lib/outo-models).
#   OUTO_CONFIG   Config file path (default /etc/outo-models/config.yaml).
#
# Exit codes:
#   0   normal exec
#   1   invalid IMAGE_FLAVOR+OUTO_ENV combination / outo-models console script missing
#   other   docker/podman exit codes

set -euo pipefail

# -----------------------------------------------------------------------------
# Start banner + version
# -----------------------------------------------------------------------------
# venv's python is on PATH (Containerfile runtime-base sets
# ENV PATH="/app/.venv/bin:/usr/local/bin:$PATH").
version=$(python -c "from outo_models.version import __version__; print(__version__)" 2>/dev/null || echo "unknown")

cat <<EOF
================================================================================
  outo-models v${version}
  IMAGE_FLAVOR=${IMAGE_FLAVOR:-stable}    OUTO_ENV=${OUTO_ENV:-production}
  DATA_DIR=${OUTO_DATA_DIR:-/var/lib/outo-models}
================================================================================
EOF

# -----------------------------------------------------------------------------
# AGENTS.md §4: reject dev image + production environment
# -----------------------------------------------------------------------------
if [[ "${IMAGE_FLAVOR:-stable}" == "dev" && "${OUTO_ENV:-production}" == "production" ]]; then
    cat >&2 <<EOF
[fatal] you are trying to run the dev image with a production environment variable.

  IMAGE_FLAVOR=dev
  OUTO_ENV=production

This combination is forbidden by AGENTS.md §4. The dev image includes
debugpy / ipython and must not be deployed to production.

Choose one of the following:
  - Use an IMAGE_FLAVOR=stable image
  - Run with OUTO_ENV=development (development machines only)
EOF
    exit 1
fi

# -----------------------------------------------------------------------------
# Pre-flight warning when non-root cannot bind 80/443 (does not fail)
# -----------------------------------------------------------------------------
# Pairs with `EXPOSE 80 443` in the Containerfile. When Caddy fails the
# bind at runtime with EPERM, this message points the operator to the
# right place to debug.
if [[ "$(id -u)" != "0" ]]; then
    # If ip_unprivileged_port_start is at or below 80, non-root can bind
    # 80 (e.g. some hosts set it to 0 and allow every port unprivileged).
    port_start=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo "32768")
    if (( port_start > 80 )); then
        cat <<'EOF'
[warn] container is running as a non-root user (uid=1000) and the kernel
       does not permit unprivileged binds to ports below 80 (i.e.
       net.ipv4.ip_unprivileged_port_start > 80). Caddy is likely to fail
       with a permission error on startup.

       Fix one of the following:
         1) podman run --cap-add NET_BIND_SERVICE ...   # recommended
         2) host port remap: -p 8080:80 -p 8443:443  # TLS termination must be handled elsewhere
       See docs/troubleshooting.md for details.
EOF
    fi
fi

# -----------------------------------------------------------------------------
# /etc/outo-models/config.yaml existence notice (not required — env vars work too)
# -----------------------------------------------------------------------------
config_path="${OUTO_CONFIG:-/etc/outo-models/config.yaml}"
if [[ ! -f "${config_path}" ]]; then
    echo "[notice] ${config_path} is missing. Configure via environment variables"
    echo "         or run the setup wizard to generate it."
fi

# -----------------------------------------------------------------------------
# outo-models console script presence check
# -----------------------------------------------------------------------------
if ! command -v outo-models >/dev/null 2>&1; then
    cat >&2 <<EOF
[fatal] cannot find the outo-models console script.

  PATH=${PATH}

The image build process must install pyproject.toml's [project.scripts]
into the venv. The venv may be corrupted, or the build may have run with
src missing.
EOF
    exit 1
fi

# -----------------------------------------------------------------------------
# exec — replace the shell with outo-models so signals are forwarded directly.
# CMD is ["serve"], and the user can override it (e.g. `podman run ... <image> migrate`)
# by passing a different subcommand; we forward "$@" as-is.
# -----------------------------------------------------------------------------
exec outo-models "$@"
