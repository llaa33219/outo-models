# syntax=docker/dockerfile:1
# outo-models — single-image build. Flavors: stable | dev
#
#   podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .
#   podman build --build-arg IMAGE_FLAVOR=dev    -t outo-models:dev    .
#
# AGENTS.md §4: image builds are *verified on a separate test machine*. In the
# dev environment we only guarantee the Containerfile is statically valid
# (hadolint, path existence, `bash -n` on every script it COPYs in).
#
# Layout, in build order:
#   builder       — uv sync --frozen --no-dev --no-editable into /app/.venv
#   caddy-builder — xcaddy with the caddy-dns/cloudflare plugin baked in
#   runtime-base  — python:3.12-slim, non-root user, runtime dirs, /app,
#                   copies venv + src + Caddyfile + rootfs + host scripts,
#                   entrypoint wrapper script. ARG IMAGE_FLAVOR is validated
#                   HERE — passing any other value aborts the build.
#   stable        — production flavor, OUTO_ENV=production, nothing extra
#   dev           — adds debugpy + ipython via pip, OUTO_ENV=development
#   final         — FROM ${IMAGE_FLAVOR}, chosen by the build arg

ARG IMAGE_FLAVOR=stable

# ---------- (1) python deps via uv ----------
FROM docker.io/library/python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
# Copy lock first so uv picks up the frozen hash on the first cache layer;
# pyproject.toml alone would force a re-resolve whenever either changed.
COPY pyproject.toml uv.lock ./
# src is required by `uv sync` (hatchling build / project metadata).
# README.md + LICENSE are referenced by pyproject.toml (readme /
# license-files) — hatchling fails the wheel build without them.
COPY src ./src
COPY README.md LICENSE ./
# --no-editable keeps the wheel installed into /app/.venv instead of an .pth
# shim, so the source tree does not need to be present at runtime.
RUN uv sync --frozen --no-dev --no-editable

# ---------- (2) caddy + cloudflare DNS plugin ----------
FROM docker.io/library/caddy:2-builder AS caddy-builder
# The custom binary lands at /usr/bin/caddy inside the builder stage; we
# copy it verbatim into runtime-base below.
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare

# ---------- (3) runtime base ----------
FROM docker.io/library/python:3.12-slim AS runtime-base
# ARG is scoped to this stage; validation RUN aborts the build on bad values.
ARG IMAGE_FLAVOR
# Hard validation — fail the build loudly if a typo slipped through.
# We do NOT silently default: an unexpected flavor almost always means the
# operator typed the wrong flag, and continuing with `stable` would mask that.
RUN if [ "$IMAGE_FLAVOR" != "stable" ] && [ "$IMAGE_FLAVOR" != "dev" ]; then \
      echo "ERROR: IMAGE_FLAVOR must be 'stable' or 'dev', got '$IMAGE_FLAVOR'" >&2; \
      exit 1; \
    fi

ENV IMAGE_FLAVOR=${IMAGE_FLAVOR} \
    OUTO_ENV=production \
    OUTO_DATA_DIR=/var/lib/outo-models \
    OUTO_CONFIG=/etc/outo-models/config.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user + dirs the app actually writes to. uid/gid fixed at 1000 so
# the named volume on the host can be chowned consistently across rebuilds.
RUN groupadd -r app -g 1000 \
    && useradd -r -g app -u 1000 -d /app -s /sbin/nologin app \
    && mkdir -p /var/lib/outo-models /etc/outo-models /opt/outo-models/scripts /app \
    && chown -R app:app /var/lib/outo-models /etc/outo-models /opt/outo-models /app

# Python venv from the uv builder. --chown ensures app owns the whole tree so
# runtime installs (the `dev` flavor) don't need root.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src /app/src
COPY --chown=app:app container/caddy/Caddyfile.j2 /opt/outo-models/caddy/Caddyfile.j2
# rootfs → / copies etc/ + usr/ trees verbatim. The entrypoint script lands
# at /usr/local/bin/outo-entrypoint.sh; the example config at
# /etc/outo-models/config.example.yaml.
COPY --chown=app:app container/rootfs/ /
# Host-side scripts (firewall-open / update / reset) live at a known path
# inside the image so the CLI can extract or invoke them via `podman run`.
COPY --chown=app:app container/scripts/ /opt/outo-models/scripts/

# Caddy binary built with the cloudflare DNS plugin.
COPY --from=caddy-builder /usr/bin/caddy /usr/local/bin/caddy

# PATH first hits the venv, then /usr/local/bin (caddy, entrypoint).
ENV PATH="/app/.venv/bin:/usr/local/bin:$PATH"

# Caddy owns 80/443 inside the container; entrypoint warns (does not fail)
# when the effective uid cannot bind them and points at docs/troubleshooting.md.
# Document the recommended host-side mitigations here so anyone reading the
# Containerfile sees them next to the EXPOSE line that triggers the question.
#   - `podman run --cap-add NET_BIND_SERVICE ...`
#   - or remap to high ports with `-p 8080:80 -p 8443:443`.
# Note: Caddy's admin API (`:2019`) and health probe (`:8080`) live on
# unprivileged ports and work without NET_BIND_SERVICE — they are NOT
# listed in EXPOSE because they are not part of the public surface.
EXPOSE 80 443

# Switching to app happens LAST so every COPY above is owned by app:app and
# no further root-only writes occur after USER.
USER app
WORKDIR /app

# Entrypoint is a shell script (not exec-form `outo-models serve` directly) so
# we can: validate the dev+production flavor/env mismatch (AGENTS.md §4),
# print the Korean startup banner, and exec the CLI with proper signal handling.
ENTRYPOINT ["/usr/local/bin/outo-entrypoint.sh"]
CMD ["serve"]

# ---------- (4) stable flavor ----------
FROM runtime-base AS stable
# Nothing extra. Production image stays minimal: no debugpy, no ipython,
# no extra packages. OUTO_ENV=production is inherited from runtime-base.
ENV OUTO_ENV=production

# ---------- (5) dev flavor ----------
FROM runtime-base AS dev
# debugpy + ipython are the only allowed deviation from stable (AGENTS.md §4).
# We briefly drop to root to pip-install into the venv, then immediately
# return to the app user so the running process stays non-root.
USER root
RUN /app/.venv/bin/pip install --no-cache-dir debugpy ipython
USER app
ENV OUTO_ENV=development

# ---------- final ----------
# The build arg selects the flavor; the trailing AS final name is just a label.
FROM ${IMAGE_FLAVOR} AS final
