# Testing

`outo-models` deliberately separates the **development environment** from
the **test environment** (AGENTS.md §4). This page explains why that split
exists and what each command verifies.

## 1. Development environment vs test environment

### Development environment (the current working machine)

- podman is **not installed** — image build / container run are not
  possible
- Integration tests run **without containers** — they exercise the real
  `git` binary and `httpx` for an in-process simulation
- Verification scope:
  - `uv sync` — dependency lock matches
  - `make lint` — ruff lint + format
  - `make typecheck` — mypy strict
  - `make test` — unit + integration tests (900+)

### Test environment (separate machine)

- podman 4.x is installed
- Run the full `setup` → `start` → `update` → `reset` flow inside a real
  container
- Validate the image build with `make build-stable` / `make build-dev`
- Static checks like `hadolint` (see the static-review comments in
  [Containerfile](../Containerfile))

## 2. `make` commands

Targets from [Makefile](../Makefile):

| Command | What it does |
| --- | --- |
| `make sync` | `uv sync --frozen` — reinstall the locked dependencies |
| `make lint` | `ruff check .` + `ruff format --check .` |
| `make format` | `ruff format .` + `ruff check --fix .` |
| `make typecheck` | `mypy src` (strict mode) |
| `make test` | `pytest` (unit + integration) |
| `make smoke` | `pytest tests/integration/test_e2e_smoke.py -v` |
| `make build-stable` | `podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .` |
| `make build-dev` | `podman build --build-arg IMAGE_FLAVOR=dev -t outo-models:dev .` |

CI must always pass `lint`, `typecheck`, and `test`. `smoke` is a subset
of the integration tests but takes longer, so it can run separately in
CI.

## 3. Test directory layout

```
tests/
├── conftest.py                    global fixtures (Settings, async engine, tmp dirs)
├── unit/
│   ├── test_config.py             Settings + env vars
│   ├── test_passwords.py          argon2id wrapper
│   ├── test_tokens.py             PASETO v4 + fingerprint
│   ├── test_sessions.py           itsdangerous sessions
│   ├── test_rate_limit.py         slowapi key functions + limits
│   ├── test_hashing.py            utils.hashing
│   ├── test_paths.py              utils.paths
│   ├── test_slug.py               utils.slug
│   ├── test_time.py               utils.time
│   ├── test_logging.py            structlog setup
│   ├── test_dns_base.py           DNSProvider ABC
│   ├── test_dns_cloudflare.py     CloudflareProvider (respx-based mock)
│   ├── test_dns_factory.py        create_provider dispatch
│   ├── test_dns_manual.py         ManualProvider
│   ├── test_firewall_detect.py    detect_firewall / is_port_open
│   ├── test_firewall_open_ports.py  open_ports + argv building
│   ├── test_caddy_manager.py      Caddyfile rendering + reload
│   ├── test_tls_renewal.py        check_cert_health + renewal_job
│   ├── test_audit_prune.py        prune_audit_logs
│   ├── test_models_*.py           each ORM model
│   ├── test_spaces_runtime.py     RuntimeState / Status mapping
│   ├── test_spaces_build.py       dulwich tree → tar, _iter_tree_blobs
│   ├── test_spaces_runtime_manager.py  Podman REST MockTransport
│   ├── test_repos_*.py            create, delete, quota, reflog, storage
│   ├── test_git_smart_auth.py     Basic auth + authorize matrix
│   ├── test_git_smart_lfs.py      LFS dispatch (locks 501 included)
│   ├── test_lfs_batch_api.py      batch API parsing + per-object decisions
│   ├── test_lfs_transfer.py       PUT/GET handler HTTP-level round-trip
│   ├── test_objectstore_local.py  LocalObjectStore (sha256 verify + symlink guard)
│   ├── test_objectstore_s3.py     S3ObjectStore (presign + sign_request)
│   ├── test_sigv4.py              AWS SigV4 vectors (path-style, presign, header)
│   ├── test_permissions.py        Scope / ROLE_SCOPES
│   └── test_container_static.py   Static checks for the Containerfile
├── integration/
│   ├── test_app_factory.py        FastAPI create_app boot
│   ├── test_alembic_migrations.py migration round-trip
│   ├── test_db_session.py         session_scope commit/rollback
│   ├── test_cli_*.py              Typer CLI via CliRunner
│   ├── test_routers_*.py          each REST router
│   ├── test_ui_pages.py           Jinja rendering + CSRF
│   ├── test_security_headers.py   response headers
│   ├── test_scheduler_jobs.py     APScheduler job bodies
│   ├── test_approval_flow.py      signup → approve → login
│   ├── test_repo_lifecycle.py     create → quota → push → reconcile
│   ├── test_spaces_registry.py    Spaces CRUD + sidecar
│   ├── test_spaces_runtime_api.py Spaces lifecycle + /run/ proxy
│   ├── test_lfs_flow.py           ASGI integration: batch → PUT → audit + add_usage
│   ├── test_git_smart_http.py     real git binary round-trip
│   └── test_e2e_smoke.py          run by `make smoke`
└── fixtures/                      static responses / certificates / git repos
    ├── certs/
    ├── dns_responses/
    └── git_repos/
```

## 4b. Test coverage added in v2

LFS / S3 / Spaces runtime required new test files so everything could be
verified without a container. **The `git-lfs` binary is not required** —
every flow runs on `httpx` and an in-process simulation.

### LFS

| File | What it covers |
| | --- |
| `tests/unit/test_git_smart_lfs.py` | `lfs_dispatch` routing, locks 501 response, method matrix |
| `tests/unit/test_lfs_batch_api.py` | `parse_batch_body` 422 cases, `dedup_objects`, `handle_batch` per-object errors (413 / 404 / 401) |
| `tests/unit/test_lfs_transfer.py` | `_handle_put` / `_handle_get` HTTP-level round-trip (sha256 / size mismatch, Content-Length cap, quota 413, 404) |
| `tests/integration/test_lfs_flow.py` | ASGI integration: `POST batch` → presigned URL/streaming PUT → `UserUsage` increment + `AuditLog("lfs.upload")` |

### ObjectStore

| File | What it covers |
| | --- |
| `tests/unit/test_objectstore_local.py` | `LocalObjectStore`'s `has_object` / `object_size` / `write_object` / `read_object` — sha256 mismatch, size mismatch, symlink guard, 64 KiB chunked stream |
| `tests/unit/test_objectstore_s3.py` | `S3ObjectStore`'s `presign_url` / `sign_request` + `aclose()` lifecycle; `__repr__` does not leak secrets |
| `tests/unit/test_sigv4.py` | AWS SigV4 reference vectors — canonical request / string-to-sign / signing key / presign query parameter order |

### Spaces runtime

| File | What it covers |
| | --- |
| `tests/unit/test_spaces_runtime.py` | Podman inspect → `RuntimeStatus` mapping (running / building / stopped / failed) |
| `tests/unit/test_spaces_runtime_manager.py` | `httpx.MockTransport` intercepts `/libpod/...` calls and verifies `start` / `stop` / `restart` / `inspect` / `list_managed` / `_allocate_host_port`. No Podman binary needed |
| `tests/unit/test_spaces_build.py` | `_iter_tree_blobs` excludes `.git` / `.hg` / `__pycache__`, `_make_tar_bytes` produces gzipped tar, `_resolve_tree_sha` works on empty repos |
| `tests/integration/test_spaces_runtime_api.py` | REST lifecycle: `POST /api/spaces` → push Dockerfile → `POST /start` → `POST /stop` → `POST /restart`; `/run/` proxy strips hop-by-hop; the `static` SDK runs without a container |

### Verification points

- **Locks 501**: `tests/unit/test_git_smart_lfs.py` pins the locks branch
  in the dispatcher.
- **Per-object error**: confirms that one object's failure still leaves
  the batch at 200, and that other healthy objects keep valid
  `actions.upload`.
- **Local vs S3 branching**: the same batch returns a same-origin href
  for `OUTO_LFS_BACKEND=local` and a presigned URL for `s3`.
- **No assumption of Podman availability**: `SpaceRuntimeManager` accepts
  a `client` argument so an httpx `MockTransport` can be injected. Tests
  exercise every call through that path, so CI passes without Podman.
- **`docker` SDK Dockerfile enforcement**: `tests/integration/test_spaces_runtime_api.py`
  verifies that a missing `Dockerfile` triggers `ValidationFailedError`.

## 5. Real git round-trip (`test_git_smart_http`)

This test is the key integration test that validates real behavior
without containers.

- Uses the `git` binary to create a temporary bare repo and a client
- Boots `GitSmartService` as an ASGI app
- Sends `/info/refs` and `git-receive-pack` requests through
  `httpx.AsyncClient`
- Verifies the response headers, the `Revision` row written after a
  successful push, and the `UserUsage` increment

`make smoke` runs only this file, so you can run a fast integration
check on a development machine with no containers.

## 6. Container behavior tests (test environment only)

On a separate machine:

```bash
# 1) Build the image
make build-stable

# 2) Prepare the data directory
sudo mkdir -p /var/lib/outo-models
sudo chown -R 1000:1000 /var/lib/outo-models

# 3) Non-interactive setup (manual DNS + skip-firewall)
sudo outo-models setup --non-interactive \
  --domain models.example.com \
  --acme-email admin@example.com \
  --dns-provider manual \
  --public-ipv4 127.0.0.1 \
  --admin-username admin \
  --admin-email admin@example.com \
  --admin-password 'changeme' \
  --skip-firewall --skip-dns --yes

# 4) start + status
sudo outo-models start
outo-models status

# 5) dry-run reset
outo-models reset

# 6) update
sudo outo-models update --image outo-models:stable
```

## 7. Checklist for new code changes

Matches AGENTS.md §6.

1. Read the relevant `docs/*.md` before making changes.
2. Add tests to `tests/unit/test_<module>.py` or
   `tests/integration/test_<module>.py` first (or together with the
   change).
3. Verify `make lint typecheck test` passes.
4. Fix the **docs** if drift appears.
5. Do not run `git commit` / `git push` until the user explicitly asks.

## Next steps

- [AGENTS.md §4](../AGENTS.md) — the dev / test environment separation
- [architecture.md](architecture.md) — module map
- [troubleshooting.md](troubleshooting.md) — issues you may hit in the
  test environment
