# AGENTS.md — outo-models development guidelines

This file defines the rules that **every** developer (human or AI agent)
working in this repository **must** follow.

## 1. Project characteristics

- **outo-models** is a fully open-source, self-hostable model hub server. It
  targets Hugging Face / ModelScope parity, and the v2 scope is **model
  sharing · dataset sharing · Spaces · Git LFS** — four features in total.
- Built on Python 3.12 + FastAPI + SQLAlchemy (async) + dulwich (git
  smart-HTTP) + Caddy (automated HTTPS/ACME). Shipped as a **single Podman
  image**.
- Every repository is git-cloneable and git-pushable
  (`git clone https://<domain>/<owner>/<name>.git`).
- Server operators manage everything through the `outo-models` CLI:
  - `setup` — first-time interactive setup (domain, DNS provider, admin
    account, ports)
  - `start` / `stop` / `restart` / `status`
  - `reset` — wipe all data and return to a freshly-installed state. The
    operator **must type "yes" exactly three times after the warning**. Do
    not weaken this safety guard for any reason.
  - `update` — pull the new image, run DB migrations, and restart
    automatically.
  - `admin ...` — approve/reject signup, ban/unban users, set storage
    quotas, assign GPUs, etc.
- "Automatic" is the project's core value. Any change that introduces a new
  manual step after install is treated as a design defect.

## 2. Development notes

1. **No security compromises.** Passwords use argon2; API tokens use PASETO
   v4; raw tokens are never stored (only hashed fingerprints are kept).
   Spray-painting `as any` / `type: ignore`, empty `except` blocks, and
   logging secrets in plaintext are all forbidden.
2. **The `reset` safety guard is immutable.** Any PR that changes the
   three-"yes" confirmation logic or the dry-run-by-default behavior will be
   rejected.
3. **The container runs non-root.** Anything requiring host privileges — such
   as opening firewall ports — must be done by **host-side scripts**
   (`container/scripts/`) invoked through the CLI, not from inside the
   container.
4. **SQLite is the default DB**, but the codebase must stay compatible with
   Postgres through SQLAlchemy. Do not write DB-specific SQL.
5. **Concurrent pushes**: repository writes are serialized with a per-repo
   `asyncio.Lock`, and per-user usage is corrected by a periodic reconcile
   job.
6. **LFS is a real implementation in v2.** `git lfs` requests are handled by
   [`src/outo_models/git_smart/lfs.py`](src/outo_models/git_smart/lfs.py)
   plus [`lfs_api.py`](src/outo_models/git_smart/lfs_api.py), exposing four
   endpoints (`/info/lfs/objects/batch`, `PUT/GET /info/lfs/objects/{oid}`).
   All LFS objects are stored through the `ObjectStore` protocol in
   [`src/outo_models/objectstore/`](src/outo_models/objectstore); the
   implementation is selected by `OUTO_LFS_BACKEND` (`local` default /
   `s3`).
   - The `local` backend shards objects to `data_dir/lfs/<aa>/<bb>/<oid>`,
     verifies sha256 + size, and atomically replaces via `os.replace`. PUT
     and GET stream directly inside the container (reusing Basic auth).
   - The `s3` backend builds presigned URLs using an in-house SigV4
     implementation (path-style, MinIO compatible) so the client uploads
     and downloads directly. The PUT/GET handlers return `501` when the S3
     backend is in use (proxied uploads are a v3 feature).
   - `OutoError("LFS locks are not supported yet")` remains a `501`. The
     `/info/lfs/locks*` endpoints land in v3.
   - `lfs_max_object_bytes` and the user's quota surface as **per-object
     errors** in the batch response; one object's failure does not fail the
     whole batch.
7. **The Spaces v2 runtime** lives in
   [`src/outo_models/spaces/runtime.py`](src/outo_models/spaces/runtime.py),
   [`runtime_manager.py`](src/outo_models/spaces/runtime_manager.py), and
   [`build.py`](src/outo_models/spaces/build.py), and runs over a Podman
   REST client.
   - It is **disabled by default** (`OUTO_SPACES_RUNTIME_ENABLED=false`).
     Operators must opt in explicitly. Once enabled, the container must be
     able to reach the Podman API socket (`OUTO_PODMAN_SOCKET`, default
     `/run/podman/podman.sock`).
   - The container runs **non-root** (uid 1000), so mount the Podman socket
     from the host (e.g.
     `-v /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro`).
     That's the standard location of a rootless Podman user socket.
   - Host ports are allocated sequentially from
     `OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` after an occupancy check
     via `list_managed()`. Only the in-container port `8000/tcp` is exposed
     to the host, and the bind IP is `127.0.0.1` (no external exposure).
   - Container identifier: `outo-space-<owner>-<name>`. Image:
     `localhost/outo-space-<owner>-<name>:latest`. Labels:
     `outo.managed=true` + `outo.space=<owner>/<name>`. Lifecycle is driven
     through Podman REST endpoints only (`v1/.../containers/{name}{create,
     start, stop, restart, remove, json}`), not `podman rm -f`.
   - The `static` SDK does not start a container; it unpacks the dulwich
     tree into `<spaces_dir>/<owner>/<name>/site/` and serves it through
     `FileResponse` (`make_build_context` and `export_static_site` share
     the same `_iter_tree_blobs`).
   - The `docker` SDK rejects the space with `ValidationFailedError` if the
     repo root is missing a `Dockerfile` or `Containerfile` (the check runs
     before `build_image`).
   - The `gradio` / `streamlit` SDKs only define the contract that the user
     supplies the base image inside the container; the code side mirrors
     the `Dockerfile` / `Containerfile` enforcement.
   - GPUs are read from `web_settings(key="gpu:<username>")` as a JSON
     array and attached to the container as `nvidia.com/gpu=<id>` CDI
     devices. On hosts without CDI, Podman rejects the device, so operators
     must install `nvidia-container-toolkit` plus the CDI specification on
     the host.
   - The proxy route `/spaces/<owner>/<name>/run/{path}` reverse-proxies to
     `http://127.0.0.1:<host_port>/<path>` only when the container is
     `running`. Hop-by-hop headers and `Content-Length` are stripped;
     failures respond with `503 space_not_running` / `504
     proxy_unreachable`.
8. Every public interface (CLI flag, REST endpoint, environment variable)
   must remain backwards-compatible. If a break is unavoidable, ship a
   migration guide in `docs/changelog.md`.

## 3. Documentation update guidelines (important)

- **When you change code, update the docs in the same commit or task.**
   Adding a CLI flag, changing an endpoint, or adding a setting requires an
   immediate update in `docs/cli.md`, `docs/admin.md`, and the relevant
   domain doc.
- **If docs and code disagree, the docs are wrong.** Do not roll back code to
   match the docs; update the docs to match the code. Exception: if the code
   is misbehaving relative to its intent, that is a bug — fix the code and
   leave the docs as is. When in doubt, raise an issue or discussion.
- Documentation is written in English. Code identifiers, command names, and
  CLI flags stay verbatim (in English) — do not translate them. Korean,
  Japanese, Chinese, or any other non-English prose in `docs/`,
  `README.md`, `AGENTS.md`, the example config comments, the systemd /
  quadlet examples, or the contract checker is a defect; translate it back
  to English.
- `scripts/check-docs.sh` verifies that every CLI command, REST router
  symbol, and `OUTO_*` environment variable is documented. Do not bypass
  this check. The `Docs/code parity` step in `.github/workflows/ci.yml`
  enforces it in CI.

## 4. Separation of development and test environments (important)

- **The current working environment is the development environment.** This
  machine does not have podman, and you must not claim to have verified
  behavior by building or running images here.
- Verification in the development environment stops at: `uv sync`,
  `make lint`, `make typecheck`, `make test` (the unit and integration
  tests run without containers, against the real `git` binary and
  `httpx`).
- **Real deployment testing happens on a separate test machine, with the
  Podman image.** If `podman build/run` does not work in the dev
  environment, do not change the code; review the `Containerfile`
  statically (hadolint, path/permission checks).
- Two image flavors exist:
  - `outo-models:stable` — production. Non-root, no debug tooling.
  - `outo-models:dev` — development. Includes debugpy / ipython;
    `OUTO_ENV=development`.
  - Build with `make build-stable` / `make build-dev` (run on the test
    machine).
- Do not deploy the `dev` image to production. Keep the entrypoint guard
  that rejects the `IMAGE_FLAVOR=dev` + `OUTO_ENV=production` combination.

## 5. Codebase map

```
src/outo_models/
  config.py, logging.py, exceptions.py   # core infrastructure
  utils/                                  # paths, slugs, time, hashing helpers
  auth/                                   # argon2, sessions, PASETO PAT, permissions, rate limit, signup approval
  db/                                     # SQLAlchemy models + Alembic migrations
  dns/                                    # DNSProvider abstraction (cloudflare, manual)
  firewall/                               # firewalld / ufw / nft detection + host-script invocation
  tls/                                    # Caddyfile rendering, reload, renewal healthcheck
  tasks/                                  # APScheduler jobs (cert, quota reconcile, audit prune)
  repos/                                  # repo disk layout, create / delete, quota
  spaces/                                 # Spaces metadata + v2 container runtime
    registry.py                            # SDK sidecar + CRUD
    runtime.py                             # RuntimeState / Status mapping
    runtime_manager.py                     # Podman REST client
    build.py                               # dulwich tree → tar + static-site export
  objectstore/                             # LFS ObjectStore protocol + backends
    base.py                                # ObjectStore + LfsAction
    local.py                               # disk backend (streaming PUT/GET)
    s3.py                                  # S3 backend (in-house SigV4, MinIO compatible)
    factory.py                             # OUTO_LFS_BACKEND dispatch
  git_smart/                              # dulwich-backed git smart-HTTP service
  server/                                 # FastAPI app, routers, middleware, Jinja templates
  cli/                                    # `outo-models` Typer CLI
  cli_remote/                             # CLI → admin REST client
container/                                # rootfs, Caddyfile template, host scripts, systemd examples
docs/                                     # English reference documentation
tests/                                    # unit / integration / fixtures
```

## 6. Workflow

1. Read the relevant docs (`docs/`) before making changes.
2. Write tests first (TDD), or at least include them in the same commit.
3. Confirm `make lint typecheck test` and the `Docs/code parity` step from
   `.github/workflows/ci.yml` (`bash scripts/check-docs.sh`) both pass.
4. If docs drift, fix the **docs** (§3). When you add a new `OUTO_*` env var
   or CLI flag, update the documentation location that
   `scripts/check-docs.sh` enforces.
5. Do not run `git commit` / `git push` until the user explicitly asks.
6. **Image releases follow the tag convention in
   `.github/workflows/release-image.yml`.** A `vX.Y.Z-stable` tag maps to
   `ghcr.io/<repo>:X.Y.Z-stable` + `:stable` + `:latest` (stable only); a
   `vX.Y.Z-dev` tag maps to `ghcr.io/<repo>:X.Y.Z-dev` + `:dev`. Container
   builds use `podman build --build-arg IMAGE_FLAVOR=stable|dev ...` only.
   Do not bypass the dev/prod combination guard from AGENTS.md §4.
