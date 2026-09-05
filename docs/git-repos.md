# git repository usage

Every outo-models repository is operated with plain git — `git clone`,
`git push`, and `git pull` are the user interface. This page describes how
[src/outo_models/git_smart](../src/outo_models/git_smart) and
[src/outo_models/repos](../src/outo_models/repos) actually behave, from
both the operator's and the user's perspective.

## URL format

Repository URLs follow the Hugging Face style.

```
https://<domain>/<owner>/<name>.git
```

Examples:

```bash
git clone https://models.example.com/alice/ll-7b.git
git clone https://models.example.com/bob/wiki-en-dataset.git
git clone https://models.example.com/alice/demo-space.git
```

The `.git` suffix is optional — the server normalizes both forms to the
same bare repo (in `git_smart.service._parse_path`).

## Authentication: Basic Auth = username + PAT

The server accepts HTTP Basic auth. **The password slot must contain a
Personal Access Token (PAT)** — regular login passwords are not accepted
on git endpoints.

```bash
# Cache credentials once
git config --global credential.helper store
git clone https://alice:<PAT>@models.example.com/alice/ll-7b.git
# Or be prompted every time
git clone https://models.example.com/alice/ll-7b.git
# Username: alice
# Password: <PAT>
```

PAT issuance:

1. Log into the web UI → user menu → **Tokens**
2. Click **Create token** → enter the name, scopes (`read`, `write`),
   and expiration
3. The response shows the plaintext once — save it immediately
4. Or via API: `POST /api/auth/tokens` (`name`, `scopes`, `ttl_days`)

## Creating a repository from the UI

The web UI exposes `GET /new` (login-gated) which renders a form
with a kind dropdown (Model / Dataset / Space), a name field
(slug-validated), a visibility selector (private / public), and an
optional description. Submitting the form POSTs
`POST /new` with the same `_csrf` double-submit cookie contract
every other UI form uses. On success the user is redirected (303)
to the new repo's overview page; on a name conflict or validation
failure the form re-renders with the error in-page and the typed
values preserved. The `space` kind delegates to
`spaces.registry.create_space` with the default `static` SDK; the
other kinds delegate to `repos.create.create_repo`.

The generated token is PASETO v4 local and expires 90 days after issuance
by default. See [security.md](security.md#personal-access-token-pat) for
the full picture.

## Repository kind

`Repo.kind` is one of three values (SQL `model` / `dataset` / `space`).

| Kind | Purpose | REST endpoint |
| --- | --- | --- |
| `model` | model weights + model card | `POST/GET/PATCH/DELETE /api/repos` |
| `dataset` | dataset files + README | `POST/GET/PATCH/DELETE /api/repos` |
| `space` | Spaces metadata (v1, static page) | `POST/GET/PATCH/DELETE /api/spaces` |

The same owner can have a `model` and a `dataset` with the same name —
the UNIQUE constraint is `(owner_id, kind, name)`, so different `kind`
values don't collide.

Create examples:

```bash
# Model
curl -X POST -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"name":"ll-7b","kind":"model","visibility":"private","description":"... "}' \
  https://models.example.com/api/repos

# Dataset
curl -X POST ... -d '{"name":"wiki-en","kind":"dataset", ...}' \
  https://models.example.com/api/repos

# Space
curl -X POST ... -d '{"name":"demo","sdk":"gradio", ...}' \
  https://models.example.com/api/spaces
```

`kind` cannot be changed after creation (`PATCH` only exposes
`visibility` / `description`). A Space's `sdk` is also immutable in v1 —
it is the contract about what runtime the repo expects.

## Visibility

- `private` — only the owner and admins can read / write
- `public` — anyone (including anonymous) can `git clone`

Anonymous clients can only:

- `git clone` / `git pull` / `git fetch` public repos (PULL only)
- `GET /api/repos`, `GET /{owner}/{name}` pages (no 404 leakage)

Anonymous `clone` against a private repo triggers a WWW-Authenticate
challenge; even with a valid PAT, non-owners get `403 Forbidden`. See the
visibility matrix in [security.md](security.md) and
[architecture.md](architecture.md).

## First push

```bash
cd my-model
git init
git remote add origin https://models.example.com/alice/my-model.git
git add .
git commit -m "initial"
git push -u origin main
```

Steps the server runs on `git push`:

1. URL → `(owner, name)` → look up the `Repo` row (404 if missing)
2. `Authorization: Basic ...` → match username / PAT → resolve the `User`
3. `authorize(user, repo, owner, PUSH)` — must be the owner or an admin
4. `check_push_allowed(session, owner, Content-Length)` — quota overflow
   → 413
5. WSGI↔ASGI adapter → dulwich handles the pack → response
6. On success (2xx):
   - Acquire `REPO_LOCKS.acquire(owner, name)` for per-repo serialization
   - Insert a `Revision` row for each newly advanced `refs/heads/*`
   - Refresh `Repo.size_bytes`
   - Increment `UserUsage.used_bytes` by the delta (clamped to zero)
   - Record `AuditLog(action="repo.push", detail=...)`

LFS requests don't go through the regular push pipeline — they go through
a separate dispatcher
([`git_smart/lfs.py`](../src/outo_models/git_smart/lfs.py)). See
[LFS usage](#lfs-usage-v2) below.

## Quota 413

`check_push_allowed` returns 413 immediately when
`used + incoming > max_bytes`. The response body is a plain-text English
message (`QuotaExceededError`).

```
HTTP/1.1 413 Request Entity Too Large
Content-Type: text/plain; charset=utf-8

quota exceeded: used=12582912000 + incoming=2147483648 > max=10737418240
```

Resolutions:

- Delete unused repos (`DELETE /api/repos/<owner>/<name>` or via the UI)
- Ask the operator for a quota bump
  (`outo-models admin quota set <name> 50GiB`)
- The hourly `quota_reconcile_job` re-measures disk usage, so you don't
  need to wait for the next tick after deleting files — the next push
  re-runs `add_usage` and corrects the drift

## LFS usage (v2)

From v2, `git lfs` is fully supported — the client workflow is unchanged
(`git lfs install`, `git lfs track "*.bin"`, `git lfs push`). The backend
is chosen via `OUTO_LFS_BACKEND` (`local` default, `s3`).

### Behavior summary

| Endpoint | Method | Handler | Notes |
| --- | --- | --- | --- |
| `/{owner}/{name}.git/info/lfs/objects/batch` | `POST` | `git_smart/lfs.py` `_handle_batch` | returns upload/download action URLs; auth + quota + size-cap checks |
| `/{owner}/{name}.git/info/lfs/objects/{oid}` | `PUT` | `_handle_put` | `local` backend only; streaming upload, sha256 verify, `add_usage` |
| `/{owner}/{name}.git/info/lfs/objects/{oid}` | `GET` | `_handle_get` | `local` backend only; 64 KiB chunked streaming |
| `/{owner}/{name}.git/info/lfs/locks*` | `*` | `lfs_not_supported` | **501** — locks land in v3 |

For the `local` backend, PUT/GET are handled **same-origin**, so `git-lfs`
reuses the Basic credentials from the original clone/push with no extra
headers. For the `s3` backend, the `actions.upload` / `actions.download`
in the batch response are **presigned URLs**, so the client talks
directly to the S3-compatible endpoint — the server's PUT/GET handlers
are not invoked (and return `501` if they are), and S3 carries the
traffic.

### Client usage

```bash
# 1) One-time LFS install + track patterns
git lfs install
git lfs track "*.safetensors"
git lfs track "*.bin"
git add .gitattributes

# 2) Push as usual — git-lfs automatically calls the batch API
git push -u origin main
# 3) Pull on another machine, also as usual
git clone https://models.example.com/alice/ll-7b.git
git lfs pull
```

Basic auth is identical to normal clone/push — username + PAT. See
[Authentication: Basic Auth = username + PAT](#authentication-basic-auth--username--pat)
for details.

### Error codes

`/info/lfs/objects/batch` expresses almost every error as **per-object**:
one object's failure does not fail the whole batch. Example response:

```json
{
  "transfer": "basic",
  "objects": [
    { "oid": "aaaa…", "size": 1048576,
      "actions": { "upload": { "href": "…", "expires_in": 3600 } } },
    { "oid": "bbbb…", "size": 2147483648,
      "error": { "code": 413, "message": "object size 2147483648 exceeds per-object limit 5368709120" } },
    { "oid": "cccc…", "size": 5242880,
      "error": { "code": 413, "message": "quota exceeded: used=… + incoming=… > max=…" } }
  ]
}
```

Status codes the batch endpoint itself may return:

| Code | Meaning | Trigger |
| --- | --- | --- |
| `200` | OK — the client iterates the entries and inspects each result |
| `406 Not Acceptable` | `Accept` header missing `application/vnd.git-lfs+json` |
| `415 Unsupported Media Type` | `Content-Type` is not LFS |
| `413 Payload Too Large` | batch body exceeds the 1 MiB cap, or PUT's `Content-Length` exceeds `OUTO_LFS_MAX_OBJECT_BYTES` |
| `422 Unprocessable Entity` | batch JSON parse failure, oid not 64-char hex, bad operation/`transfers` |
| `401 Unauthorized` | Basic credentials missing / invalid |
| `403 Forbidden` | authenticated but not authorized (private repo + non-owner) |
| `404 Not Found` | repo missing, or `GET /objects/{oid}` with a missing object |
| `500 Internal Server Error` | configuration error (e.g. `OUTO_LFS_BACKEND=s3` with `OUTO_S3_ENDPOINT` empty) |

The locks endpoints (`/info/lfs/locks/*`) always respond with:

```
HTTP/1.1 501 Not Implemented
Content-Type: application/json
Cache-Control: no-store

{"error": "Git LFS locks are not supported yet", "docs": "/docs/git-lfs"}
```

### Backend configuration (`OUTO_LFS_BACKEND`)

Default is `local`. `local` shards objects to
`OUTO_DATA_DIR/lfs/<aa>/<bb>/<oid>` — no extra setup. To use an
S3-compatible store (such as MinIO), fill in the following per the S3
backend description in [`architecture.md`](architecture.md#lfs-request-flow):

```yaml
# /etc/outo-models/config.yaml (excerpt of the relevant keys)
lfs_backend: s3
s3_endpoint: http://minio.local:9000
s3_bucket: outo-lfs
s3_region: us-east-1
# Inject OUTO_S3_ACCESS_KEY / OUTO_S3_SECRET_KEY via env vars, not YAML
s3_prefix: lfs
s3_presign_ttl_seconds: 3600
```

Migration steps:

1. Create the `outo-lfs` bucket in MinIO and mint an access key / secret
   key
2. The MinIO daemon on a separate host must be reachable as `s3_endpoint`
   from the `outo-models` container's network — the network setup is the
   host's responsibility
3. Set `lfs_backend: s3` in `config.yaml` and restart
4. If you have existing local LFS objects, copy them to MinIO with
   `mc cp --recursive` and keep the same oid layout at
   `<s3_prefix>/<aa>/<bb>/<oid>` to stay compatible

See the [MinIO documentation](https://min.io/docs/minio/linux/index.html)
and [security.md §LFS auth model](security.md#lfs-auth-model) for more.

## Concurrency

- Per-repo `asyncio.Lock` (`RepoLockRegistry.REPO_LOCKS`) serializes
  concurrent pushes against the same repo — dulwich's on-disk state and
  the DB's `Revision` / `UserUsage` stay consistent
- Pushes against different repos proceed in parallel
- The hourly `quota_reconcile_job` re-measures `disk_usage` for every user
  and corrects any `UserUsage` drift

## Next steps

- [spaces.md](spaces.md) — creating a Space repo
- [admin.md](admin.md) — quota / ban operations
- [security.md](security.md) — authentication mechanisms in detail
