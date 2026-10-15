# Spaces

Spaces are the Hugging Face-style repo kind for hosting interactive demos.
From v2, the **container runtime** is supported in-tree, so you can build
and launch gradio / streamlit / docker SDK demos. This page is a direct
mirror of [`src/outo_models/spaces/`](../src/outo_models/spaces)'s
runtime behavior.

## Scope

- **Repository**: `Repo(kind="space")` — reuses the same infra as regular
  git repos
- **Metadata**: `<data_dir>/spaces/<owner>/<name>.json` sidecar (`sdk`,
  `updated_at`)
- **REST**: `/api/spaces/*` (create / list / detail / update / delete +
  runtime lifecycle)
- **Runtime (v2)**: Podman REST API for build / start / stop / restart /
  delete. `OUTO_SPACES_RUNTIME_ENABLED` toggles the whole thing.
- **Proxy**: `/spaces/<owner>/<name>/run/{path:path}` — reverse-proxies to
  the container's `8000/tcp`.

`SUPPORTED_SDKS` is the four-tuple
`("static", "gradio", "streamlit", "docker")`. If `sdk` is not in the list
at creation time, the request is rejected with
`NotFoundError("unsupported sdk: '<x>'")`. `PATCH` exposes only
`visibility` / `description` — `sdk` cannot change (changing it would
break the runtime contract).

## Create

```bash
curl -X POST -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"name":"demo","sdk":"gradio","visibility":"public","description":"..."}' \
  https://models.example.com/api/spaces
```

Response (`SpaceSummary`):

```json
{
  "id": 42,
  "name": "demo",
  "sdk": "gradio",
  "visibility": "public",
  "description": "...",
  "owner": "alice",
  "clone_url": "https://models.example.com/alice/demo.git",
  "created_at": "2026-08-31T00:00:00+00:00"
}
```

Internal steps:

1. Verify `sdk` is in `SUPPORTED_SDKS` (`NotFoundError` otherwise)
2. `outo_models.repos.create.create_repo(kind="space")` — creates the bare
   repo, the `Repo` row, the quota row, and the `repo.create` audit log
3. Write the `<spaces_dir>/<owner>/<name>.json` sidecar (`sdk`,
   `updated_at`)
4. Commit the transaction

`SDK` decisions:

- **`static`** (default): no container is launched; the dulwich tree is
  unpacked into `<spaces_dir>/<owner>/<name>/site/` and served through
  `FileResponse`. No build queue, no Podman calls.
- **`gradio`**, **`streamlit`**: the contract is that the user supplies a
  base image inside the container; the code side behaves the same as the
  `docker` SDK (Podman build / start).
- **`docker`**: rejected with `ValidationFailedError` on `start` /
  `restart` if the repo root is missing a `Dockerfile` or `Containerfile`.

## Detail

```bash
curl https://models.example.com/api/spaces/alice/demo
```

`SpaceDetail` (`SpaceSummary` plus a `runtime` block):

```json
{
  "id": 42, "name": "demo", "sdk": "gradio",
  "visibility": "public", "description": "...",
  "owner": "alice",
  "clone_url": "https://models.example.com/alice/demo.git",
  "created_at": "2026-08-31T00:00:00+00:00",
  "runtime": {
    "state": "running",
    "message": "Space is running.",
    "url": "https://models.example.com/spaces/alice/demo/run/",
    "container_id": "abcdef…",
    "port": 20314
  }
}
```

`runtime.state` possible values:

| state | Meaning | Next action |
| --- | --- | --- |
| `disabled` | `OUTO_SPACES_RUNTIME_ENABLED=false` (operator disabled) | start/stop/restart all return 503 |
| `stopped` | container missing or exited/stopped | call `start` to launch it |
| `building` | `podman build` is in progress (inferred from Podman response) | will become `running` / `failed` shortly |
| `running` | container is `running` and a host port is allocated | access `/spaces/<owner>/<name>/run/` |
| `failed` | Podman call failed or container is unhealthy | audit log `space.<action>` records `error_code` |

## Runtime lifecycle

Three POST endpoints drive the container. All of them authenticate via
the current session (cookie + `get_current_user`) — do not call with a PAT
only.

| Endpoint | Method | Behavior |
| --- | --- | --- |
| `/api/spaces/{owner}/{name}/start` | `POST` | `build_image()` → `start()`. The `static` SDK only runs `export_static_site` |
| `/api/spaces/{owner}/{name}/stop` | `POST` | `manager.stop()` — Podman `containers/{name}/stop?t=0` |
| `/api/spaces/{owner}/{name}/restart` | `POST` | The `static` SDK re-runs `export_static_site`; everything else does stop → build_image → start |
| `/api/spaces/{owner}/{name}/status` | `GET` | Maps Podman inspect output to `RuntimeStatus` (also open to anonymous reads) |

### Space runtime UI surface

The Space page follows the Hugging Face Spaces layout: the **Runtime tab is
the default and first view**. Opening `/{owner}/{name}` for a `kind=space`
repo lands on `/{owner}/{name}/runtime`.

Tab order for a Space:

```
Runtime | Space card | Files | Community | Settings
```

`Settings` is owner/admin only. Non-space repos keep the old order and are
unaffected: `Model card | Files | Community | Settings`.

| Route | Purpose |
| --- | --- |
| `GET /{owner}/{name}/runtime` | The Runtime tab. Spaces only; a model or dataset repo gets `404` |
| `GET /{owner}/{name}/card` | The README card tab for a Space (the `Space card` tab above) |
| `GET /{owner}/{name}` | Runtime tab for a Space, the card for everything else |

The Runtime tab carries:

1. A **full-viewport run iframe** of the path-relative
   `/spaces/{owner}/{name}/run/` — the Space gets the whole panel, not a
   sidebar tile. The old right-sidebar runtime tile is gone from this tab.
2. A **big Start / Stop button** (owner-only, and only when
   `settings.spaces_runtime_enabled` is `True`).
3. A **large error panel** that takes over the run area when the state is
   `failed`, or when a form failure redirected back (see the query
   parameters below).
4. On the *other* tabs (card, files, community) a **compact status tile**
   stays in the sidebar, in the same BLP Minimal Tile chrome as the other
   info / downloads / collections / owner tiles (square 2px border, 0
   radius, instant border hover).

The compact status tile still shows a state chip, the SDK label
(`sdk: <value>`), and the `RuntimeStatus.message` from
[`runtime_status(...)`](../src/outo_models/spaces/runtime.py):

- `stopped`  → paper-blue-2 background, muted text
- `building` → paper-blue background, sky border
- `running`  → paper-green background, green border, deep-dark text
- `failed`   → soft-red border, deep-red text
- `disabled` → muted, inactive border

When the runtime is **disabled**, the page surfaces an
`OUTO_SPACES_RUNTIME_ENABLED=true` admin hint instead of any buttons — the
operator's hint stays visible to all viewers.

### Start / stop forms

Start and stop are form POSTs on the Space page:

- `POST /{owner}/{name}/space/start`
- `POST /{owner}/{name}/space/stop`

Both are CSRF-protected (`_csrf` cookie), login-required (anonymous →
`303 /login?next=...`), and 403 for non-owners (including other logged-in
users — same domain check the JSON API uses). Both delegate to the JSON
API's `_run_lifecycle` helper, so audit logs and the Podman call path are
identical.

> The form POSTs are intentionally separate from the JSON API POSTs
> at `/api/spaces/.../start` because the page form needs CSRF
> protection and same-tab redirect semantics, while the JSON API
> stays stateless + token-friendly.

### Form outcomes in the URL

A form POST always redirects back to the page, and the outcome rides in the
query string instead of being swallowed:

| Query | Meaning |
| --- | --- |
| `?started=1` | `start` succeeded |
| `?stopped=1` | `stop` succeeded |
| `?runtime_error=<code>&message=<url-quoted>` | The action failed; the Runtime tab renders the large error panel with `message` |

`<code>` is the `OutoError` `.code` — for example `podman_unreachable`,
`space_build_failed`, `runtime_disabled` — or `internal_error`. Codes are
the stable contract; the message text is a human-readable detail.

### `static` SDK status

A `sdk: static` Space never consults Podman. The status is derived from the
exported site only:

- site exported → `running`
- site not exported → `stopped`

So a static Space can no longer report `failed` because of a Podman
connection error, and `POST /api/spaces/{owner}/{name}/stop` returns
`stopped` without touching Podman and without re-exporting the site.

Each action:

1. `_ensure_runtime_enabled(settings)` — returns `503 runtime_disabled`
   if the runtime is off
2. Loads `Repo` and checks owner / admin
3. `REPO_LOCKS.acquire(owner, name)` to serialize concurrent actions on
   the same Space
4. Calls into `SpaceRuntimeManager`
5. Records `AuditLog(action="space.<action>", detail={ok,
   state/error_code})`

For the exact REST paths used by lifecycle methods, see
[Podman REST paths](#podman-rest-paths).

### Example `start` response

```bash
curl -X POST -b cookies.txt \
  https://models.example.com/api/spaces/alice/demo/start
```

```json
{
  "state": "running",
  "message": "Space is running.",
  "url": "https://models.example.com/spaces/alice/demo/run/",
  "container_id": "abcdef…",
  "port": 20314
}
```

`port` is allocated sequentially from
`OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` (default 20000..21000). The
in-container port is fixed at `8000/tcp`.

### The `run/` proxy

When the container is `running`, all five HTTP methods are supported:

```
GET    /spaces/<owner>/<name>/run/{path:path}
POST   /spaces/<owner>/<name>/run/{path:path}
PUT    /spaces/<owner>/<name>/run/{path:path}
PATCH  /spaces/<owner>/<name>/run/{path:path}
DELETE /spaces/<owner>/<name>/run/{path:path}
```

- Hop-by-hop headers (`connection`, `keep-alive`, `transfer-encoding`,
  `host`, `content-length`, etc.) and `Authorization`-class headers are
  stripped before delegating
- If the container is not running: `503 space_not_running`
- If the upstream is dead: `504 proxy_unreachable` (httpx
  `RequestError`)
- The `static` SDK does not proxy — it returns
  `_file_response_for_static(site_dir, "...")` directly. If `{path}` is
  empty or ends in `/`, the handler falls back to `index.html`.

## GPU assignment

The JSON array under `web_settings(key="gpu:<username>")` is attached as
`nvidia.com/gpu=<id>` CDI devices when the container is created. The
operator assigns GPUs with `outo-models admin gpu assign alice gpu-0`;
on `start`, the user's GPU list is forwarded to the container.

- On hosts without CDI, Podman rejects the device and raises
  `OutoError(code="podman_api", status_code=502)`.
- Assigning multiple GPUs (`gpu-0 gpu-1`) attaches all of them.
- Build runs on a GPU-less host, so GPU assignments only affect the
  runtime container, not the build stage.

## Clone / push

Spaces are cloned and pushed just like regular git repos.

```bash
git clone https://models.example.com/alice/demo.git
cd demo
echo '# Demo' > README.md
git add . && git commit -m "init"
git push -u origin main
```

Permissions / quota / LFS policies are identical to
[git-repos.md](git-repos.md).

## Configuration (`OUTO_*`)

| Environment variable | Meaning | Default |
| --- | --- | --- |
| `OUTO_SPACES_RUNTIME_ENABLED` | Enable the runtime | `true` |
| `OUTO_PODMAN_SOCK` | **Host** path to the Podman socket, read by the host shim and used by `outo-models start` as the mount source. The rootless user socket is preferred. | (auto-detected) |
| `OUTO_PODMAN_SOCKET` | Podman socket path **inside the container**, consumed by the server (Spaces runtime) and by the CLI's `podman-remote` | `/run/podman/podman.sock` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` | Start of host port range | `20000` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_END` | End of host port range | `21000` |

### The socket is mounted for you

`outo-models start` probes the host for a Podman socket and mounts it into
the server container automatically. Its `podman run` gains:

```bash
-v <host-socket-path>:/run/podman/podman.sock:ro
-e OUTO_PODMAN_SOCKET=/run/podman/podman.sock
```

The host path comes from `OUTO_PODMAN_SOCK` when set (the shim forwards the
host path through it), otherwise from probing `$XDG_RUNTIME_DIR/podman/podman.sock`
first and `/run/podman/podman.sock` second. Rootless-first matters: the
server container runs as uid 1000 and cannot connect to a rootful
`root:root 0660` socket. When no socket is found at all, `start` prints:

```
[note] no Podman socket found — Spaces container SDKs (docker/gradio/streamlit) stay unavailable until a Podman socket is mounted.
```

When only a rootful socket exists, the shim warns:

```
[warn] only a rootful Podman socket was found; the server container (uid 1000) may get EACCES.
       enable the rootless user socket: systemctl --user enable --now podman.socket
```

Deployments that bypass `outo-models start` — quadlet and anything else that
launches the container itself — must mount the socket themselves:

```ini
Volume=%t/podman/podman.sock:/run/podman/podman.sock:ro
Environment=OUTO_PODMAN_SOCKET=/run/podman/podman.sock
```

`%t` is the invoking user's runtime dir (`/run/user/<uid>`), i.e. the
rootless socket. The
[quadlet example](../container/examples/quadlet/outo-models.container)
carries both lines.

> **The socket mount is the trust boundary.** The host Podman daemon grants
> the in-container process full container create/delete rights — for a
> rootless socket that is effectively host-root-equivalent within that
> user's namespace. This is inherent to running user-supplied app
> containers; there is no safe way to grant `docker` / `gradio` /
> `streamlit` Spaces without it. Note that `:ro` on a Unix socket is
> **not** a security control (the flag does not apply to socket
> permissions); it is there for tidiness only. The real controls are the
> non-root uid, the read-only root filesystem, and host-side namespace
> isolation. See
> [security.md §Spaces runtime isolation](security.md#spaces-runtime-isolation).

## Podman REST paths

`SpaceRuntimeManager`
([`spaces/runtime_manager.py`](../src/outo_models/spaces/runtime_manager.py))
calls only the following paths (with the `/v4.0.0/libpod` prefix):

| Action | HTTP |
| --- | --- |
| occupancy check | `GET /v4.0.0/libpod/containers/json?all=true&filter=label=outo.managed=true` |
| create container | `POST /v4.0.0/libpod/containers/create` |
| start | `POST /v4.0.0/libpod/containers/{name}/start` |
| stop | `POST /v4.0.0/libpod/containers/{name}/stop?t=0` |
| restart | `POST /v4.0.0/libpod/containers/{name}/restart` |
| force delete | `DELETE /v4.0.0/libpod/containers/{name}?force=true&ignore=true` |
| inspect | `GET /v4.0.0/libpod/containers/{name}/json` |
| build image | `POST /v4.0.0/libpod/build?t=<tag>` (Content-Type `application/x-tar`) |

Images are tagged `localhost/outo-space-<owner>-<name>:latest`, container
names are `outo-space-<owner>-<name>`. The two namespaces are managed
solely through the `outo.managed=true` + `outo.space=<owner>/<name>`
labels — they do not collide with other containers / images on the host
Podman.

## Troubleshooting

For detailed error handling see the corresponding sections of
[troubleshooting.md](troubleshooting.md):

- **503 `runtime_disabled`** — `OUTO_SPACES_RUNTIME_ENABLED=true` was not
  passed to the container. Verify with
  `podman inspect outo-models --format '{{.Config.Env}}'`.
- **503 `podman_unreachable`** — Podman socket not mounted, or permission
  denied. Inside the container, run
  `curl --unix-socket /run/podman/podman.sock \
  http://d/v4.0.0/libpod/containers/json` to check reachability.
- **502 `space_build_failed`** — Podman build failed. The last 2 KiB of
  the response message is a slice of the Podman build log
  (`_build_failure_tail`); debug your `Dockerfile` / `Containerfile`
  against that.
- **503 `space_not_running`** — calling `/run/` while the container is
  not running. Check `/api/spaces/<owner>/<name>/status` and call `start`.
- **503 "all runtime ports in use"** — raise
  `OUTO_SPACES_RUNTIME_PORT_RANGE_END` or `stop` unused Spaces.

## Next steps

- [git-repos.md](git-repos.md) — clone / push and LFS flow
- [security.md](security.md) — Spaces runtime isolation / Podman socket
  risks
- [troubleshooting.md](troubleshooting.md) — Podman / LFS error response
  catalog
