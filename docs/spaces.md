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
| `OUTO_SPACES_RUNTIME_ENABLED` | Enable the runtime | `false` |
| `OUTO_PODMAN_SOCKET` | Podman REST API Unix socket path | `/run/podman/podman.sock` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` | Start of host port range | `20000` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_END` | End of host port range | `21000` |

In production, mount the host Podman socket when launching the container:

```bash
# rootless Podman (user 1000) user socket
podman run -d --name outo-models \
  -v /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro \
  -e OUTO_SPACES_RUNTIME_ENABLED=true \
  ...
```

> **The socket mount is the trust boundary.** The host Podman daemon grants
> the in-container process full container create/delete rights. The
> minimum safety baseline is a non-root container, a read-only mount, and
> host-side namespace isolation (userns). See
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
