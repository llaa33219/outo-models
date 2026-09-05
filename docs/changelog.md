# Changelog

Every public-interface change to `outo-models` is recorded here. Per
AGENTS.md §2.8, CLI flags / REST endpoints / environment variables stay
backwards-compatible, and breaking changes ship with a migration guide.

## v0.3.0 — Social layer + HF-style repo pages + PAT web UI

Release date: (unreleased — dev builds only)

### Added

- **Repository social layer**: likes (`RepoLike`), user follows
  (`UserFollow`), per-repo comments (`RepoComment`), and a clone/fetch
  counter (`Repo.downloads_count`) — migration `0002_social`.
- **HF-style repository page**: header (owner/name, copy-clone-URL button,
  like + follow capsules), tabs as shareable URLs (Model/Dataset card ·
  Files · Community), and a right sidebar (downloads, card metadata,
  collections placeholder, owner tile).
- **Model/Dataset card rendering**: `README.md` rendered from the bare repo
  via dulwich (markdown → sanitized HTML via mistune) with YAML
  front-matter metadata (task, license, tags, datasets, base_model,
  language) surfaced in the sidebar.
- **Files tab**: dulwich tree listing (dirs first, traversal-safe).
- **Access tokens page** (`/settings/tokens`): create/list/revoke PATs,
  token shown once with a copy button, git username+PAT usage guidance.
  This is the supported way to get git push credentials — account
  passwords are NOT accepted on git endpoints.
- **Logout**: `Log out` navbar link → confirm tile → CSRF-protected POST
  clears the session cookie.
- **UI redesign** to the BLP Minimal Tile language (`디자인.md`):
  square 2px tiles, capsule elements, BLP palette, Pretendard
  (CSP gained `font-src` for the font CDN).

### Fixed

- `start` verifies the server actually answers `/healthz` (with a
  plain-language diagnosis on failure) instead of trusting
  `podman run -d`.
- CSRF token consistency on form pages (first-visit empty token /
  reload mismatch both fixed).

## v0.2.0 — LFS · S3 · Spaces runtime

Release date: 2026-09-01

The v2 release lifts v1's metadata-only scope into something operationally
useful. All v0.1.0 public interfaces continue to work as before.

### Added features

#### Full Git LFS support

The previous version returned a 501 stub for LFS; this release replaces
it with a complete implementation.

- Four endpoints are live:
  - `POST /{owner}/{name}.git/info/lfs/objects/batch` — issues
    upload / download action URLs
  - `PUT /{owner}/{name}.git/info/lfs/objects/{oid}` — streaming upload
  - `GET /{owner}/{name}.git/info/lfs/objects/{oid}` — 64 KiB chunked
    streaming
- Authentication: reuses the Basic PAT from regular clone/push (handled
  by `git-lfs` automatically)
- Per-object errors: a single object's failure (size / quota / 404)
  does not fail the whole batch — per the LFS spec
- sha256 + size verification, then atomic rename via `os.replace`
- Symlink guard: if any segment of `_object_path` is a symlink, both
  reads and writes are rejected (cuts off path traversal at the entry
  point)
- Only `/info/lfs/locks*` remains a 501 — coming in v3
- Full flow: [git-repos.md §LFS usage](git-repos.md#lfs-usage-v2)

#### LFS backend: `local` / `s3`

[`OUTO_LFS_BACKEND`](cli.md#environment-variables) selects between two
backends.

- `local` (default): shards objects to `OUTO_DATA_DIR/lfs/<aa>/<bb>/<oid>`.
  No additional infrastructure required.
- `s3`: AWS S3 / MinIO / R2 / other S3-compatible stores. Direct upload /
  download through presigned URLs. In-house SigV4 implementation
  (path-style, MinIO compatible) — no `boto3` / `aioboto3` dependency.
  Configuration details:
  [git-repos.md §Backend configuration](git-repos.md#backend-configuration-outo_lfs_backend),
  [security.md §`s3` backend presigned URLs](security.md#s3-backend-presigned-urls).

New environment variables:

- `OUTO_LFS_MAX_OBJECT_BYTES` (default 5 GiB)
- `OUTO_S3_ENDPOINT`, `OUTO_S3_BUCKET`, `OUTO_S3_REGION` (default
  `us-east-1`)
- `OUTO_S3_ACCESS_KEY`, `OUTO_S3_SECRET_KEY`
- `OUTO_S3_PREFIX` (default `lfs`)
- `OUTO_S3_PRESIGN_TTL_SECONDS` (default 3600)

#### Spaces runtime (Podman)

The v2 runtime in [`src/outo_models/spaces/`](../src/outo_models/spaces):

- Disabled by default. Enable explicitly with
  `OUTO_SPACES_RUNTIME_ENABLED=true`.
- `OUTO_PODMAN_SOCKET` (default `/run/podman/podman.sock`) connects to
  the Podman REST API. Inside the container the socket is reached over a
  Unix domain socket (`httpx.AsyncHTTPTransport(uds=...)`).
- Lifecycle: `start` / `stop` / `restart` / `status`, with audit logs
  (`space.start` / `space.stop` / `space.restart`).
- Container naming `outo-space-<owner>-<name>`, image tag
  `localhost/outo-space-<owner>-<name>:latest`, labels
  `outo.managed=true` + `outo.space=<owner>/<name>`.
- Host ports allocated sequentially from
  `OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` (default 20000..21000).
  The in-container port is fixed at `8000/tcp` and bound to `127.0.0.1`
  (no external exposure).
- GPUs: the JSON array under `web_settings(key="gpu:<username>")` is
  attached as `nvidia.com/gpu=<id>` CDI devices.
- Per-SDK behavior:
  - `static` — no container; the dulwich tree is unpacked into
    `spaces/<owner>/<name>/site/` and served through `FileResponse`.
  - `gradio` / `streamlit` — the user supplies the base image inside the
    container; the code side mirrors the `docker` SDK.
  - `docker` — rejected with `ValidationFailedError` if the repo root is
    missing a `Dockerfile` or `Containerfile`.
- Proxy `/spaces/<owner>/<name>/run/{path}` — supports all five methods
  (GET/POST/PUT/PATCH/DELETE). Only proxies when the container is
  running, to `http://127.0.0.1:<port>/<path>`. Hop-by-hop headers are
  stripped.

#### License

[LICENSE](../LICENSE) (Apache-2.0) added. v0.1.0 had no license file,
which made redistribution ambiguous; v0.2.0 makes the terms explicit
under Apache-2.0.

#### CI / image-release workflows

Two GitHub Actions are added.

- `.github/workflows/ci.yml` — main / PR trigger. Enforces ruff + mypy
  + pytest + `scripts/check-docs.sh`.
- `.github/workflows/release-image.yml` — `vX.Y.Z-stable` /
  `vX.Y.Z-dev` tag trigger. After tests pass, builds **natively per
  architecture** (amd64 on `ubuntu-24.04`, arm64 on `ubuntu-24.04-arm` —
  no QEMU), pushes per-arch tags `:X.Y.Z-<flavor>-amd64` / `-arm64`, then
  combines them into manifest lists: `:X.Y.Z-<flavor>`, `:stable` /
  `:dev`, and (for stable only) `:latest`. A plain `podman pull` on an
  ARM server resolves to arm64 automatically. Tag convention:
  [architecture.md §CI/CD](architecture.md#cicd).

### New environment variables summary

`OUTO_LFS_BACKEND`, `OUTO_LFS_MAX_OBJECT_BYTES`, `OUTO_S3_ENDPOINT`,
`OUTO_S3_BUCKET`, `OUTO_S3_REGION`, `OUTO_S3_ACCESS_KEY`,
`OUTO_S3_SECRET_KEY`, `OUTO_S3_PREFIX`, `OUTO_S3_PRESIGN_TTL_SECONDS`,
`OUTO_SPACES_RUNTIME_ENABLED`, `OUTO_PODMAN_SOCKET`,
`OUTO_SPACES_RUNTIME_PORT_RANGE_START`,
`OUTO_SPACES_RUNTIME_PORT_RANGE_END`. All have defaults, so upgrading
requires no migration.

### Migration guide

Upgrading to v0.2.0 **requires no migration steps**. The defaults for
every new environment variable preserve the v0.1.0 behavior (and LFS no
longer returns the v0.1.0 501 + roadmap message — it actually works in
v0.2.0; this is a **feature addition**, not a behavior change).

> **Note**: any client that depended on v0.1.0's LFS 501 response (e.g.
> custom download scripts) will start receiving real LFS responses in
> v0.2.0. Operators that need LFS disabled should leave
> `OUTO_LFS_BACKEND` set to `local` and block it at the proxy. There is
> no flag to disable LFS itself.

When you launch a new container without enabling the Spaces runtime (the
default), every Space behaves like v0.1.0 — `runtime.state = "disabled"`,
`/run/` returns `503 runtime_disabled`.

## v0.1.0 — Initial release

Release date: 2026-08-31

The first release of v1. Contains every core feature needed to
self-host a Hugging Face / ModelScope-style, git-based model hub.

### Install / operations

- Single `outo-models` Typer console script entry point (`console_scripts`
  in `pyproject.toml`)
- Interactive / non-interactive setup wizard (`outo-models setup`)
- Container lifecycle: `start` / `stop` / `restart` / `status`
- Image refresh + DB migration + restart (`outo-models update`)
- Three-`yes` gate on `outo-models reset` (dry-run by default, requires
  `OUTO_DESTRUCTIVE=1`)
- Podman single image (`outo-models:stable`, `outo-models:dev`) — runs
  non-root
- AGENTS.md §4 enforcement: rejects the `dev + production` combination
- Quadlet systemd unit example + opt-in host-side firewall auto-open
  unit
- Host-side scripts (`firewall-open.sh`, `update.sh`, `reset.sh`)

### DNS / TLS

- DNS provider abstraction + Cloudflare / Manual implementations
- Cloudflare mode: automatic DNS A record creation + DNS-01 ACME
  challenge
- Manual mode: English instructions + operator confirmation prompt
- Caddy (in-container, 80/443) + automatic ACME issuance / renewal
- Staging switch supported via
  `acme-staging-v02.api.letsencrypt.org/directory` (`TlsConfig.staging = True`)
- Daily `cert_renewal_job` (00:00 UTC) checks certs and nudges Caddy

### Data / DB

- SQLAlchemy 2.x async + aiosqlite (default) / Postgres-compatible
  (only swap the DB URL)
- Alembic migrations (`alembic upgrade head`)
- 9 tables: `users`, `repos`, `revisions`, `personal_access_tokens`,
  `approvals`, `user_quotas`, `user_usages`, `audit_logs`,
  `web_settings`
- Per-repo `asyncio.Lock` serializes concurrent pushes
- Hourly `quota_reconcile_job` corrects per-user usage
- Daily `audit_prune_job` (02:00 UTC) deletes audit logs older than 90
  days

### Authentication / authorization

- argon2id password hash (`time_cost=3`, `memory_cost=64 MiB`)
- PASETO v4 local PAT (plaintext never stored, only argon2id
  fingerprint)
- itsdangerous `URLSafeTimedSerializer` session cookie (`outo_session`)
- 7-day session, rotated on every login
- CSRF double-submit cookie (UI forms)
- slowapi rate limits: login `5/minute`, signup `3/minute`, git push /
  pull, API — all per-IP / per-user buckets
- Security headers auto-applied: HSTS, CSP, X-Frame-Options,
  Permissions-Policy, etc.

### git / REST

- FastAPI + REST routers: `auth`, `users`, `repos`, `spaces`, `admin`,
  `webhooks`, UI
- dulwich-backed git smart-HTTP — URL:
  `https://<domain>/<owner>/<name>.git`
- HTTP Basic auth = username + PAT
- Authorization matrix: PUSH is owner / admin only; PULL allows anonymous
  for public, owner / admin for private
- Quota overflow → `413`, LFS → `501` (stub + roadmap link)
- After push: record `Revision`, refresh `Repo.size_bytes`, reconcile
  `UserUsage`, log `AuditLog(action="repo.push")`

### Models / datasets / Spaces

- `RepoKind`: `model` / `dataset` / `space`
- `Visibility`: `public` / `private`
- Spaces v1: metadata + static pages + `SUPPORTED_SDKS = {static, gradio,
  streamlit, docker}`. Runtime reports `preview_unavailable` (v2 roadmap)

### Admin features

- Signup flow: `pending` → `approved` / `denied` (+ `unban` to take
  `banned` back to `approved`)
- `admin list` / `pending` / `approve` / `deny` / `ban` / `unban` /
  `reset-password`
- Per-user storage quota (`quota show` / `set`; accepts `10GiB`-style
  input)
- Free-form GPU ID labels (`gpu show` / `assign` / `clear`)
- Remote delegation via `--api-url` + `--token` against
  `/api/admin/*` (except `reset-password`, which is local-only)

### Docs / automation

- English docs under `docs/` (including this changelog)
- `scripts/check-docs.sh` automatically verifies CLI commands /
  environment variables / docs consistency (usable as a CI gate like
  `make lint`)
- 750+ pytest cases (unit + integration, no containers required)
- ruff + mypy strict + bandit static analysis

### Known limitations

- LFS (`git lfs`) is a 501 stub only — split large objects manually for
  now
- Spaces container runtime not supported — `runtime.state` always
  reports `preview_unavailable`
- Webhook endpoints only expose `/api/webhooks/test` — formal integrations
  land in v2
- `debugpy` / `ipython` exposure on the `dev` image is intentional
  (development image only)
- Auto-updates rely on the quadlet `AutoUpdate=registry` policy — the
  host's `podman-auto-update.timer` must be active

### Migration guide

This is the first release, so there is no prior version to migrate from.
v0.0.x does not exist.

## Next-version roadmap

- Full LFS support (`git lfs` API + chunked object store)
- Spaces v2 runtime (container isolation + build queue + resource
  limits)
- Formal webhook integration (push / repo.created / user.signup events)
- Metrics / Prometheus exporter
- Auto-update stabilization (in-place migration)

Each item bumps the minor / major version here when it ships.
