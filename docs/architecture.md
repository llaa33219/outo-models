# Architecture

This page helps operators build a mental model of how the system behaves.
Implementation details evolve with the code, so whenever the doc drifts from
the code we update the doc (AGENTS.md §3).

## Module map

```
src/outo_models/
├── config.py, logging.py, exceptions.py      core infrastructure
├── utils/                                     paths, slugs, time, hashing
├── auth/                                      argon2, sessions, PASETO PAT, permissions, rate limit
│   ├── approval.py                            signup approval state machine
│   ├── passwords.py                           argon2id wrapper
│   ├── permissions.py                         scope / role
│   ├── rate_limit.py                          slowapi Limiter
│   ├── sessions.py                            itsdangerous session cookies
│   └── tokens.py                              PASETO v4 local + fingerprint
├── db/
│   ├── engine.py, session.py                  SQLAlchemy async engine / session
│   ├── models/                                ORM models
│   └── migrations/                            Alembic migrations
├── dns/
│   ├── base.py                                DNSProvider ABC + DnsRecord
│   ├── cloudflare.py                          Cloudflare implementation
│   ├── factory.py                             create_provider dispatch
│   └── manual.py                              manual mode (prints instructions)
├── firewall/
│   ├── detect.py                              firewalld / ufw / nftables detection
│   └── open_ports.py                          host-script invocation
├── tls/
│   ├── caddy_manager.py                       Caddyfile rendering + admin API
│   └── renewal.py                             certificate healthcheck + nudge
├── tasks/
│   ├── scheduler.py                           APScheduler wrapper
│   └── jobs/                                  cert_renewal / quota_reconcile / audit_prune
├── repos/
│   ├── models.py                              RepoKind / Visibility domain models
│   ├── storage.py                             disk layout + per-repo asyncio.Lock
│   ├── create.py, delete.py                   bare repo creation / deletion
│   ├── quota.py                               UserQuota / UserUsage + reconcile
│   └── reflog.py                              recent commit lookup
├── spaces/
│   ├── registry.py                            SDK sidecar + CRUD
│   ├── runtime.py                             RuntimeState / Status (Podman inspect mapping)
│   ├── runtime_manager.py                     SpaceRuntimeManager (Podman REST)
│   └── build.py                               dulwich tree → tar + static-site export
├── objectstore/
│   ├── base.py                                ObjectStore Protocol + LfsAction
│   ├── local.py                               disk backend (sha256 verification + atomic rename)
│   ├── s3.py                                  S3 backend (in-house SigV4, MinIO compatible)
│   └── factory.py                             OUTO_LFS_BACKEND dispatch
├── git_smart/
│   ├── service.py                             GitSmartService (dulwich adapter)
│   ├── auth.py                                Basic auth + authorize matrix
│   ├── lfs.py                                 LFS dispatch + PUT/GET handlers
│   └── lfs_api.py                             LFS batch API parsing + response building
├── server/
│   ├── app.py                                 create_app (FastAPI factory)
│   ├── middleware.py                          SecurityHeadersMiddleware
│   ├── deps.py                                get_db / get_current_user / require_admin
│   ├── errors.py                              exception → JSON envelope
│   ├── routers/                               auth, users, repos, spaces, admin, webhooks, ui
│   └── templates/                             Jinja HTML templates
├── cli/                                       outo-models Typer CLI
│   ├── setup/                                 interactive wizard
│   ├── admin/                                 user / quota / GPU management
│   ├── start.py, stop.py, restart.py, status.py  container lifecycle
│   ├── update.py, reset.py                    update / reset
│   └── server.py                              in-container serve / migrate
└── cli_remote/                                AdminApiClient (remote admin mode)
```

The `container/` directory holds the host-side glue.

```
container/
├── caddy/Caddyfile.j2                         Jinja Caddyfile template
├── rootfs/                                    file tree copied into the container image
│   ├── etc/outo-models/config.example.yaml
│   └── usr/local/bin/outo-entrypoint.sh
├── scripts/
│   ├── firewall-open.sh                       manipulate host firewall
│   ├── update.sh                              pull + migrate + restart
│   └── reset.sh                               container / volume cleanup
├── examples/quadlet/                          podman systemd quadlet example
└── systemd/outo-models-host.service           boot-time firewall auto-open (opt-in)
```

## Data layout

The default root is `OUTO_DATA_DIR` (default `/var/lib/outo-models`). The
same path is used inside the container (via a Podman volume mount).

```
/var/lib/outo-models/
├── db.sqlite3                      SQLite (or Postgres via OUTO_DB_URL)
├── repos/                          bare git repositories
│   └── <owner>/
│       └── <name>.git/              bare repo created by dulwich
├── lfs/                            local LFS backend (OUTO_LFS_BACKEND=local)
│   └── <aa>/<bb>/<oid>             sha256 oid, 2-level sharded
├── spaces/                         Spaces sidecar + static-site export
│   └── <owner>/
│       ├── <name>.json             { "sdk": "static" | "gradio" | "streamlit" | "docker", ... }
│       └── <name>/site/            files exported by the static SDK (when applicable)
├── certs/                          ACME certificate cache (populated by Caddy)
└── audit/                          audit logs (currently stored inside the DB)
```

`utils.paths.ensure_dirs()` creates all five directories idempotently.

## Database schema

The single Alembic migration in v1
([src/outo_models/db/migrations/versions/0001_initial.py](../src/outo_models/db/migrations/versions/0001_initial.py))
creates the following tables.

| Table | Key columns | Notes |
| --- | --- | --- |
| `users` | `id`, `username` (UNIQUE), `email` (UNIQUE), `password_hash`, `role` (`user`/`admin`), `status` (`pending`/`approved`/`denied`/`banned`), `display_name`, `approved_at`, `approved_by_id` | `status` is the signup-flow state machine |
| `repos` | `id`, `owner_id` FK, `name`, `kind` (`model`/`dataset`/`space`), `visibility`, `description`, `default_branch`, `size_bytes`, `path` | UNIQUE `(owner_id, kind, name)` |
| `revisions` | `id`, `repo_id` FK, `commit_sha`, `branch`, `author_id` FK, `message`, `size_bytes` | populated by git smart-HTTP after a push |
| `personal_access_tokens` | `id`, `user_id` FK, `name`, `fingerprint_hash` (argon2id), `prefix`, `scopes` (JSON), `expires_at`, `last_used_at` | raw tokens are not stored |
| `approvals` | `id`, `user_id` FK UNIQUE, `decision`, `reason`, `decided_by_id` FK, `decided_at` | signup decision trail |
| `user_quotas` | `id`, `user_id` FK UNIQUE, `max_bytes` | operator-set |
| `user_usages` | `id`, `user_id` FK UNIQUE, `used_bytes` | populated by reconcile |
| `audit_logs` | `id`, `actor_id` FK, `action`, `target_type`, `target_id`, `detail`, `ip`, `created_at` | every admin action / push / signup |
| `web_settings` | `id`, `key` UNIQUE, `value`, `created_at`, `updated_at` | free-form keys/values (e.g. GPU assignment) |

## Request flow

### External client → Caddy → uvicorn

```
browser / git CLI
        │  HTTPS (80 → Caddy, 443 → Caddy)
        ▼
Caddy (in-container) :80/:443
        │  - ACME issuance/renewal (HTTP-01 or DNS-01 cloudflare)
        │  - TLS termination
        │  - reverse_proxy 127.0.0.1:8000
        ▼
uvicorn (127.0.0.1:8000) ← outo-models server serve
        │  lifespan: run_migrations + TaskScheduler.start
        │
        ├── /api/*                       FastAPI routers (auth/users/repos/spaces/admin/webhooks)
        │       │
        │       └── SecurityHeadersMiddleware (HSTS / CSP / X-Frame-Options ...)
        │       └── SlowAPIMiddleware (rate limit)
        │       └── get_current_user / require_admin deps
        │
        ├── /, /login, /signup, /admin/*  UI routers (Jinja2 + CSRF double-submit)
        │
        └── /{owner}/{name}.git/...      GitSmartService (root mount)
                │
                ├── if rest == info/lfs/*  → lfs_dispatch (separate flow, see below)
                ├── resolve Repo + owner from DB
                ├── resolve_git_identity (Basic <b64(username:PAT)>)
                ├── authorize(user, repo, owner, action)
                ├── if PUSH: check_push_allowed → 413 on quota
                ├── _WsgiToAsgi → dulwich.web.HTTPGitApplication
                └── on PUSH success: per-repo lock + record Revision + AuditLog
```

Internal / IP mode (`Settings.is_internal=True`) drops the TLS layer:

- Caddy binds plain `:80` only — no `email` / `acme_ca` / per-site
  `tls { ... }` blocks in the rendered Caddyfile.
- The security-headers middleware suppresses `Strict-Transport-Security`
  so the browser doesn't refuse plain-HTTP requests.
- `tls.renewal.renewal_job` short-circuits with
  `CertHealth(ok=True, …)` — no `:443` handshake is attempted.

The Caddyfile switch is owned by `TlsConfig.tls_enabled` (default
`True`), which `TlsConfig.from_settings(settings, …)` derives from
`Settings.is_internal`. The middleware reads `settings.is_internal`
directly. `Settings.base_url` uses the same flag to pick `http://` vs
`https://`.

### LFS request flow

The path `/{owner}/{name}.git/info/lfs/*` is dispatched to
`lfs_dispatch` in
[`git_smart/lfs.py`](../src/outo_models/git_smart/lfs.py) instead of going
through dulwich.

```
git-lfs client
        │
        ▼
Caddy (:443) → uvicorn (127.0.0.1:8000)
        │
        ▼
GitSmartService.asgi_app
        │  _is_lfs(rest) ⇒ True
        ▼
lfs_dispatch(scope, receive, send, ...)
        │
        ├── POST /info/lfs/objects/batch
        │       │  _handle_batch
        │       ├── Accept / Content-Type check (406 / 415)
        │       ├── body ≤ 1 MiB (413)
        │       ├── parse_batch_body → BatchRequest (422 on shape)
        │       ├── _load_repo(owner, repo)
        │       ├── resolve_git_identity(Basic ...)
        │       ├── authorize(PUSH or PULL)
        │       ├── dedup_objects
        │       ├── create_object_store(settings)
        │       │       │
        │       │       └── OUTO_LFS_BACKEND=local → LocalObjectStore
        │       │           (data_dir/lfs/<aa>/<bb>/<oid> + same-origin href)
        │       │       │
        │       │       └── OUTO_LFS_BACKEND=s3    → S3ObjectStore
        │       │           (presigned URL via in-house SigV4, path-style)
        │       │
        │       └── handle_batch → entries per object
        │           ├─ already exists → respond without actions
        │           ├─ size > lfs_max_object_bytes → per-object error(code=413)
        │           ├─ check_push_allowed (over quota) → per-object error(code=413)
        │           └─ OK → store.make_upload_action / make_download_action
        │
        ├── PUT /info/lfs/objects/{oid}
        │       │  _handle_put (LocalObjectStore only)
        │       ├── Content-Length > lfs_max_object_bytes → 413
        │       ├── check_push_allowed (per-user quota) → 413
        │       ├── LocalObjectStore.write_object (sha256 + size verify, atomic rename)
        │       ├── add_usage(owner, written)
        │       └── AuditLog(action="lfs.upload")
        │
        ├── GET /info/lfs/objects/{oid}
        │       │  _handle_get (LocalObjectStore only)
        │       ├── visibility check (public anonymous OK, private owner/admin)
        │       └── LocalObjectStore.read_object → 64 KiB chunk stream
        │
        └── /info/lfs/locks*  →  lfs_not_supported (501 + JSON)
```

Key points:

- **Per-object errors**: a single object's failure (size cap, quota, 404)
  does not fail the whole batch. `error.code` is an integer per the LFS
  spec (`413`, `404`).
- **S3 backend**: the client talks to S3 directly through the presigned
  URL, so the server never receives the PUT/GET traffic and never
  increments usage. The server's PUT/GET handlers are `local`-backend
  only — if invoked with `OUTO_LFS_BACKEND=s3`, they return `501` (the
  `local` and `s3` paths are explicitly separate).
- **Local-backend symlink guard**: `LocalObjectStore` rejects both reads
  and writes if any segment of `_object_path` is a symlink. `oid` only
  accepts 64-char hex, so path traversal is cut off at the entry point.
- **Quota coupling**: `check_push_allowed` is invoked twice — at the batch
  stage and at the PUT stage — to stay safe against quota drift between
  the batch prediction and the actual upload.

### ObjectStore protocol

`ObjectStore`
([`objectstore/base.py`](../src/outo_models/objectstore/base.py)) exposes
the five methods handlers depend on — the only interface they rely on.

| Method | Return / behavior |
| --- | --- |
| `make_upload_action(*, owner, repo, oid, size)` | `LfsAction` (href / headers / expires_in) |
| `make_download_action(*, owner, repo, oid, size)` | `LfsAction` |
| `has_object(oid)` | `bool` — symlinks always return False |
| `object_size(oid)` | `int \| None` — None if missing or symlinked |
| `delete_object(oid)` | idempotent deletion |

`LocalObjectStore` additionally exposes the server-side helpers
`write_object` / `read_object` for the PUT/GET handlers. `S3ObjectStore`
has no such helpers — it only returns presigned URLs.

### Spaces runtime flow

```
user  ─ POST /api/spaces/<owner>/<name>/start ─▶  Caddy → uvicorn
                                                          │
                                                          ▼
                                              routers/spaces.py: start_space
                                                          │
                                                          ├── _ensure_runtime_enabled
                                                          ├── get_space
                                                          ├── owner/admin check
                                                          ├── _load_owner_gpu_ids  (web_settings)
                                                          ├── SpaceRuntimeManager(settings)
                                                          └── _run_lifecycle (REPO_LOCKS serialize)
                                                              │
                                                              ├── sdk="static"
                                                              │     └─ export_static_site(owner, name,
                                                              │                         spaces_dir/.../site/)
                                                              │
                                                              └── sdk ∈ {gradio, streamlit, docker}
                                                                    │
                                                                    ├─ docker SDK: Dockerfile/Containerfile present?
                                                                    │     └─ missing → ValidationFailedError
                                                                    │
                                                                    ├─ manager.build_image(owner, name)
                                                                    │     ├─ make_build_context (dulwich tree → tar)
                                                                    │     └─ POST /v4.0.0/libpod/build?t=<tag>
                                                                    │         └─ failure → 502 space_build_failed
                                                                    │
                                                                    ├─ manager._allocate_host_port  (20000..21000)
                                                                    │
                                                                    └─ manager.start(owner, name, gpu_ids=...)
                                                                          │
                                                                          ├─ POST /v4.0.0/libpod/containers/create
                                                                          │     + body.PortBindings[8000/tcp]
                                                                          │     + hostConfig.devices: CDI GPU
                                                                          ├─ POST /v4.0.0/libpod/containers/{name}/start
                                                                          └─ return (container_id, host_port)
                                                          │
                                                          ▼
                                              runtime_status_async(inspect)
                                                          │
                                                          ▼
                                              JSON { state, message, url, container_id, port }
```

All traffic to `/spaces/<owner>/<name>/run/{path}` is delegated to the
container only when the lifecycle allows it (`proxy_router`).

```
GET /spaces/<owner>/<name>/run/foo/bar
        │
        ▼
  _proxy_dispatch
        ├── _viewer_can_see (visibility)
        ├── sdk="static"  → _file_response_for_static(site_dir, "foo/bar") → FileResponse
        ├── !spaces_runtime_enabled  → 503 runtime_disabled
        ├── manager.inspect(owner, name)
        │     └── not "running" → 503 space_not_running
        │
        └── _stream_proxy_response(GET, http://127.0.0.1:<host_port>/foo/bar, ...)
                ├── strip hop-by-hop headers
                ├── httpx.AsyncClient.request(...)
                └── StreamingResponse(upstream.body, upstream.status_code, cleaned headers)
```

### CI/CD

`.github/workflows/` contains two workflows.

#### `ci.yml` — main / PR trigger

Forced on every PR and push to `main`:

1. `uv sync --frozen`
2. `ruff check .` + `ruff format --check .`
3. `mypy src`
4. `pytest` (unit + integration)
5. `bash scripts/check-docs.sh` — docs ↔ code parity (CLI commands,
   `OUTO_*` environment variables, and the index.md TOC). This gate must
   pass before merging.

#### `release-image.yml` — tag trigger

A Git tag matching `vX.Y.Z-stable` or `vX.Y.Z-dev` triggers the workflow.
Both flavors are built for **two architectures** — natively, never under
QEMU emulation: `amd64` on `ubuntu-24.04`, `arm64` on `ubuntu-24.04-arm`
(free for public repos). The per-arch images are then combined into a
manifest list, so a plain `podman pull` always resolves to the host arch.

```
vX.Y.Z-stable  →  IMAGE_FLAVOR=stable  →  ghcr.io/<repo>:X.Y.Z-stable        (manifest: amd64+arm64)
                                              ghcr.io/<repo>:stable          (manifest)
                                              ghcr.io/<repo>:latest          (manifest, stable only)
                                              ghcr.io/<repo>:X.Y.Z-stable-amd64 / -arm64  (per-arch)
vX.Y.Z-dev     →  IMAGE_FLAVOR=dev     →  ghcr.io/<repo>:X.Y.Z-dev           (manifest)
                                              ghcr.io/<repo>:dev             (manifest)
                                              ghcr.io/<repo>:X.Y.Z-dev-amd64 / -arm64     (per-arch)
```

Job graph: `test` (`pytest` + `check-docs.sh`) → `build-arch` (matrix:
amd64 + arm64; each builds with
`podman build --platform linux/<arch> --build-arg IMAGE_FLAVOR=<flavor>`,
pushes the per-arch tag, and smoke-runs `--help` on the same-arch runner)
→ `manifest` (pulls both per-arch images, creates and pushes the
`X.Y.Z-<flavor>` and `<flavor>` manifest lists, plus `latest` for stable).

> **The tag convention is recommended by AGENTS.md §6.6.** Any other tag
> format is rejected with `Tag '...' must match vX.Y.Z-stable or vX.Y.Z-dev`.

### CLI invocation flow (host)

```
outo-models <subcommand>
        │
        ▼
Typer app (cli/main.py) — OutoError → English one-liner + exit 1
        │
        ├── setup / update / start / stop / restart / status
        │       │
        │       └── setup → _collect (prompts) → _effect (config.yaml, DNS, firewall, DB, admin)
        │       │     ├── internal mode (--domain empty / IP): skip ACME/DNS prompts,
        │       │     │   skip the DNS step, render Caddyfile with tls_enabled=False
        │       │     ├── firewall step tolerates `firewall_container_host_required`
        │       │     │   (prints the host command, continues) so the install completes
        │       │     │   even when the wizard runs via the host shim
        │       │     └── all other steps are unchanged
        │       └── update → src/outo_models/assets/scripts/update.sh
        │       └── start  → podman run (config.yaml driven)
        │       └── stop/restart/status → podman calls
        │
        └── admin → _commands → _local_db (SQL) | AdminApiClient (HTTP)
```

## Quota model

- `UserQuota.max_bytes` — set by the operator (default
  `OUTO_DEFAULT_QUOTA_BYTES`)
- `UserUsage.used_bytes` — increases immediately after push, decreases
  immediately on delete
- The hourly `quota_reconcile_job` re-measures `disk_usage` for every
  user and corrects `UserUsage.used_bytes` (drift correction)
- `check_push_allowed` raises `QuotaExceededError` (413) when
  `used + incoming > max`, so the push itself is rejected
- `Repo.size_bytes` is refreshed after a push from
  `disk_usage(repo_fs_path)`

Code locations:
[src/outo_models/repos/quota.py](../src/outo_models/repos/quota.py),
[src/outo_models/tasks/jobs/quota_reconcile.py](../src/outo_models/tasks/jobs/quota_reconcile.py).

## Scheduler jobs

`TaskScheduler` (an APScheduler wrapper) registers three jobs. Each one
runs with `max_instances=1`, `coalesce=True`, and
`misfire_grace_time=3600` to avoid overlap and absorb delay.

| ID | Trigger | Body | What it does |
| --- | --- | --- | --- |
| `cert_renewal` | daily 00:00 UTC | `cert_renewal_job` | TLS handshake on `<domain>:443` → `CertHealth`; if unhealthy and Caddy is reachable, nudge Caddy with a reload |
| `quota_reconcile` | hourly | `quota_reconcile_job` | re-measure `disk_usage` for every user and correct `UserUsage` |
| `audit_prune` | daily 02:00 UTC | `prune_audit_logs` | delete `AuditLog` rows older than 90 days (default retention: 90 days) |

None of the three jobs ever raise. Transient errors are logged as structlog
warnings and retried on the next tick.

## Image flavors

The `IMAGE_FLAVOR` ARG in the `Containerfile` selects one of two final
targets.

- `outo-models:stable` — production. `OUTO_ENV=production`. No debugpy /
  ipython.
- `outo-models:dev` — development. `OUTO_ENV=development`. Includes
  debugpy + ipython.

The entrypoint (`/usr/local/bin/outo-entrypoint.sh`) enforces the
following guard (AGENTS.md §4):

> The `IMAGE_FLAVOR=dev` + `OUTO_ENV=production` combination is rejected
> and exits 1.

Otherwise both flavors behave identically (packages, network policy, disk
layout, etc.).

### Quadlet example

[container/examples/quadlet/outo-models.container](../container/examples/quadlet/outo-models.container)
is a podman systemd quadlet example. In production, only the following
need adjustment:

- `Image=` — `outo-models:stable` (production) or `:dev` (test)
- `PublishPort=` — keep 80, 443 (see [troubleshooting.md](troubleshooting.md)
  for loopback mapping notes)
- `Volume=outo-models-data:/var/lib/outo-models` — do not rename (static
  test enforces the name)
- Beyond `Environment=OUTO_ENV=production`, keep secrets in `systemd-creds`
  or another external secret store

## Boot-time firewall auto-open (opt-in)

[container/systemd/outo-models-host.service](../container/systemd/outo-models-host.service)
is an opt-in helper that opens 80 / 443 on the host side at boot, before
the container is launched.

```bash
sudo cp container/systemd/outo-models-host.service /etc/systemd/system/
sudo systemctl edit outo-models-host.service   # adjust OUTO_FIREWALL_KIND
sudo systemctl enable --now outo-models-host.service
```

It defaults to `disabled`. Leave it alone if you don't need it.

## Next steps

- [security.md](security.md) — authentication, tokens, rate limits
- [git-repos.md](git-repos.md) — git request handling in detail
- [testing.md](testing.md) — which tests verify which flow
