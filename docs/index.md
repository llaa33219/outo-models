# outo-models documentation

outo-models is a Hugging Face / ModelScope-style, git-based model hub you can
self-host, shipped as a single Podman image. This documentation set is
written so an operator can handle everything from initial install through
day-to-day operations and incident response on their own.

When the documentation and the code disagree, **the documentation** is wrong
(AGENTS.md §3). The repository verifies CLI commands, environment variables,
and cross-doc consistency automatically through `scripts/check-docs.sh`.

## Table of contents

- [Install](install.md) — pull or build the image and run it for the first
  time
- [Setup wizard](setup-wizard.md) — every prompt of `outo-models setup` and
  the automated steps
- [CLI reference](cli.md) — single source of truth for every command, flag,
  and environment variable
- [Administrator guide](admin.md) — signup approval, bans, quotas, GPUs,
  remote mode
- [Architecture](architecture.md) — module map, request flow, data layout,
  CI/CD
- [Security](security.md) — argon2, PASETO PAT, sessions, CSRF, rate limits,
  LFS, Spaces isolation
- [DNS providers](dns-providers.md) — Cloudflare automatic mode and the
  manual fallback
- [git repositories](git-repos.md) — clone / push, PAT usage, quotas, LFS
  policy
- [Spaces](spaces.md) — v2 runtime lifecycle, Podman integration, GPUs, the
  reverse proxy
- [Troubleshooting](troubleshooting.md) — common operational issues (Podman,
  LFS, S3 included)
- [Testing](testing.md) — `make lint/typecheck/test/smoke` and the integration
  test scope
- [Changelog](changelog.md) — release notes for v0.1.0 and v0.2.0

## Quick start

This guide assumes podman is installed on the server host (verify with
`podman --version`). See [install.md](install.md) for full details.

```bash
# 1) Pull the image (recommended: :stable from ghcr.io)
sudo podman pull ghcr.io/<owner>/outo-models:stable

# Or build it locally (on the test machine)
make build-stable          # outo-models:stable
make build-dev             # outo-models:dev (development only)

# 2) Initial setup (interactive wizard)
outo-models setup

# 3) Operate
outo-models start
outo-models status
outo-models restart
outo-models update

# 4) Full reset (requires three 'yes' confirmations)
outo-models reset --destroy      # together with OUTO_DESTRUCTIVE=1
```

After `setup` finishes you can browse to `https://<domain>/`, and any git
client can clone / push to `https://<domain>/<owner>/<name>.git`. See
[setup-wizard.md](setup-wizard.md) and [git-repos.md](git-repos.md) for the
full flow.

## Environment variable quick reference

The variables you encounter most often are listed below. For the complete
definition of every `OUTO_*` variable, see the [CLI reference](cli.md#environment-variables).

| Variable | Meaning | Default |
| --- | --- | --- |
| `OUTO_DATA_DIR` | Data directory (DB, git repos, LFS, cert cache) | `/var/lib/outo-models` |
| `OUTO_DOMAIN` | Public domain the service responds on | `localhost` |
| `OUTO_DB_URL` | DB URL (empty → `${OUTO_DATA_DIR}/db.sqlite3`) | (derived) |
| `OUTO_SECRET_KEY` | Session / token signing key (32+ chars in production) | (none) |
| `OUTO_ENV` | Runtime environment (`development` / `production`) | `development` |
| `OUTO_REQUIRE_APPROVAL` | Require admin approval on new signups | `true` |
| `OUTO_DEFAULT_QUOTA_BYTES` | Default storage quota for new users | `10737418240` (10 GiB) |
| `OUTO_LFS_BACKEND` | LFS backend (`local` / `s3`) | `local` |
| `OUTO_LFS_MAX_OBJECT_BYTES` | Max size of a single LFS object | `5368709120` (5 GiB) |
| `OUTO_S3_ENDPOINT` / `OUTO_S3_BUCKET` / `OUTO_S3_REGION` | S3 backend endpoint, bucket, region | (none / none / `us-east-1`) |
| `OUTO_S3_ACCESS_KEY` / `OUTO_S3_SECRET_KEY` | S3 credentials — inject via env vars only | (none) |
| `OUTO_S3_PREFIX` / `OUTO_S3_PRESIGN_TTL_SECONDS` | S3 object-key prefix, presign TTL | `lfs` / `3600` |
| `OUTO_SPACES_RUNTIME_ENABLED` | Spaces container runtime on/off | `false` |
| `OUTO_PODMAN_SOCKET` | Podman REST API Unix socket | `/run/podman/podman.sock` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` / `_END` | Space container host-port range | `20000` / `21000` |
| `OUTO_CONFIG` | YAML config path (used by CLI host side) | `/etc/outo-models/config.yaml` |
| `OUTO_DESTRUCTIVE` | Safety gate that unlocks `reset --destroy` | (none) |
| `OUTO_CLOUDFLARE_API_TOKEN` | Used by Cloudflare mode to create DNS records | (none) |
| `OUTO_CADDY_ADMIN_URL` | Caddy admin API base URL | `http://localhost:2019` |
| `CLOUDFLARE_API_TOKEN` | Used inside the Caddy process for DNS-01 | (none) |

## Next steps

- First-time operator → [install.md](install.md) → [setup-wizard.md](setup-wizard.md)
- Operator managing permissions, storage, GPUs → [admin.md](admin.md)
- User pushing models via git → [git-repos.md](git-repos.md) (includes LFS)
- Building a Space → [spaces.md](spaces.md) (Podman runtime)
- Stuck on an issue → [troubleshooting.md](troubleshooting.md) (Podman / LFS / S3)
- Looking for release notes → [changelog.md](changelog.md)
