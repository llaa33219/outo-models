# Security

outo-models's security policy is the AGENTS.md §2 "no security compromises"
principle implemented in code. This page is the single place operators
look to see which protections are automatic and which settings they must
not touch.

## 1. Passwords

Storage: [src/outo_models/auth/passwords.py](../src/outo_models/auth/passwords.py).

- Algorithm: **argon2id** (`argon2-cffi` wrapper)
- Parameters: `time_cost=3`, `memory_cost=64 MiB`, `parallelism=1` (per
  the OWASP Password Storage Cheat Sheet)
- Storage format: PHC string (`$argon2id$...`) — algorithm, parameters,
  and salt are all encoded, so parameter upgrades stay compatible
- Every call generates a fresh random salt — the same password hashes
  differently each time
- Verification failures (`VerificationError`, `InvalidHashError`) never
  raise; they always return `False` (no user enumeration)
- `needs_rehash` returning `True` triggers automatic rotation after a
  successful login

Password policy:

- Signup form: minimum 8 characters (Pydantic `min_length=8`)
- Admin password: validated by the setup wizard (8+ chars) and re-entered
  for confirmation

When the operator needs to recover a lost password, run
`outo-models admin reset-password <name>`. The plaintext is printed to
stdout exactly once.

## 2. Personal Access Token (PAT)

Storage: [src/outo_models/auth/tokens.py](../src/outo_models/auth/tokens.py).

- Token format: **PASETO v4 local** (encrypted + authenticated)
- Key derivation: `Settings.secret_key` → `sha256(secret)` → 32-byte
  PASETO key
- Token plaintext is **never stored in the DB**
- The DB only holds `fingerprint_hash` (argon2id via
  `utils.hashing.hash_secret`) and `prefix` (first 8 chars)
- Default lifetime: `DEFAULT_TOKEN_TTL_SECONDS = 7_776_000` (90 days)
- The creation response carries the plaintext once; there is **no way to
  retrieve it later**

Verification flow:

1. `Authorization: Basic <b64(username:token)>` (git) or
   `Bearer <token>` (API)
2. `match_fingerprint(pat.fingerprint_hash, token)` against each
   candidate PAT row
3. On match, `last_used_at` is refreshed
4. If the user is `banned` / `pending`, the auth result is coerced to
   `None`

The create / revoke / list endpoints match the `POST/GET/DELETE
/api/auth/tokens` rows in [cli.md](cli.md).

## 3. Session cookies

Storage: [src/outo_models/auth/sessions.py](../src/outo_models/auth/sessions.py).

- Library: `itsdangerous.URLSafeTimedSerializer`
- Cookie name: `outo_session` — do not rename (breaks client
  compatibility)
- Salt: `outo-models.session.v1` (prevents reuse of the token for other
  purposes)
- Payload: `{ "user_id": <int>, "nonce": <secrets.token_urlsafe(16)> }`
- Lifetime: 7 days (`_SESSION_MAX_AGE_SECONDS`)
- **A new token is issued on every login (rotation)** — defeats session
  fixation attacks

`cookie_kwargs(secure)` defines every cookie attribute in one place.

- `HttpOnly=True` — JS access blocked (XSS mitigation)
- `SameSite="Lax"` — top-level GET navigations are allowed (OIDC-style
  flows)
- `Path="/"` — available across all paths
- `Secure=True` (production) / `False` (development) — chosen from
  `Settings.env`

HSTS: when `domain` is a real hostname (anything that isn't an IP
literal), responses automatically include
`strict-transport-security: max-age=31536000; includeSubDomains`. In
internal / IP mode (`Settings.is_internal=True`) HSTS is suppressed
because the server speaks plain HTTP — a browser that cached HSTS would
refuse the very request the operator is trying to make.

## 4. CSRF

Storage: [src/outo_models/server/routers/_ui_helpers.py](../src/outo_models/server/routers/_ui_helpers.py).

UI forms are protected by double-submit cookies.

1. `GET /signup` and `GET /login` issue an `_csrf` cookie
2. The same value is rendered into the form as `<input name="_csrf">`
3. `POST /signup` and `POST /login` compare the cookie and the form value
   with `secrets.compare_digest`
4. Mismatch / missing value → 403

The CSRF token is signed by itsdangerous with the salt
`outo-models.csrf.v1` (separate from the session cookie salt). API
endpoints (`/api/*`) are not CSRF targets — browsers don't auto-attach
cookies there, so authentication is enforced by explicit tokens / Basic
auth instead.

## 5. Rate limits

Storage: [src/outo_models/auth/rate_limit.py](../src/outo_models/auth/rate_limit.py).

| Constant | Value | Applied endpoints |
| --- | --- | --- |
| `LOGIN_LIMIT` | `5/minute` | `POST /api/auth/login` |
| `SIGNUP_LIMIT` | `3/minute` | `POST /api/auth/signup` |
| `GIT_PUSH_LIMIT` | `30/minute` | git receive-pack (defined only; enforced in v2) |
| `GIT_PULL_LIMIT` | `120/minute` | git upload-pack (defined only; enforced in v2) |
| `API_LIMIT` | `240/minute` | default REST API |

Key functions:

- `key_by_ip` — per-IP buckets (login / signup)
- `key_by_user_or_ip` — authenticated users get `user:<id>`, otherwise
  IP, so legitimate users behind NAT don't crowd into one bucket

Slowapi's `RateLimitExceeded` returns a JSON 429 response when the limit
is hit.

## 6. Security headers

Storage: [src/outo_models/server/middleware.py](../src/outo_models/server/middleware.py).

Every response (including git smart-HTTP streams) gets the headers below
automatically.

| Header | Value |
| --- | --- |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` |
| `Content-Security-Policy` | `default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (only for real hostnames — internal / IP mode suppresses HSTS) |

The CSP allows `'unsafe-inline'` for `style-src` only — the bundled Jinja
templates use inline `<style>` blocks. `script-src` stays `'self'`; no
inline script is ever shipped.

The Space run proxy path `/spaces/{owner}/{name}/run/**` is the one scoped
exception: it sends `X-Frame-Options: SAMEORIGIN` and a CSP of
`frame-ancestors 'self'` only, because the framed document is user-supplied
application content whose own scripts must run. See
[§The Podman socket mount is the trust boundary](#the-podman-socket-mount-is-the-trust-boundary).

## 7. Audit log

Storage: the `audit_logs` table.

- `actor_id` FK — which user (or `None` for system actions like signup)
- `action` — `user.signup`, `user.approve`, `user.deny`, `user.ban`,
  `user.unban`, `repo.create`, `repo.push`, `admin.quota`, `admin.gpu`,
  `admin.reset_password`
- `target_type`, `target_id` — the kind and PK of the target
- `detail` — JSON string (e.g. quota old/new, push branch advances)
- `ip` — requester IP (filled by the router; CLI paths are not recorded
  today)
- `created_at` — UTC timestamp

The `audit_prune` job runs daily at 02:00 UTC and deletes rows older than
90 days. To change retention, edit `_DEFAULT_RETENTION_DAYS` in
`tasks/jobs/audit_prune.py` or call `prune_audit_logs(retention_days=N)`
directly.

## 8. Caddy and ACME

Storage: [src/outo_models/tls/caddy_manager.py](../src/outo_models/tls/caddy_manager.py),
[src/outo_models/assets/caddy/Caddyfile.j2](../src/outo_models/assets/caddy/Caddyfile.j2).

- Caddy owns ports 80 / 443 inside the container and handles ACME
  issuance / renewal
- HTTP-01: regular domains when port 80 is reachable from the public
  internet for ACME
- DNS-01: in Cloudflare mode, `tls { dns cloudflare {env.CLOUDFLARE_API_TOKEN} }`
- `OUTO_TLS_STAGING=true` (or the equivalent flag) switches to the Let's
  Encrypt staging CA — useful for avoiding rate-limit losses from a typo
  in the domain
- **Internal / IP mode** (`Settings.is_internal=True`): Caddy binds
  plain `:80` and the rendered Caddyfile drops the global `email` /
  `acme_ca` / per-site `tls { … }` blocks. The `:8080/healthz` health
  probe listener is preserved. The wizard passes
  `TlsConfig.tls_enabled=not answers.is_internal` so the renderer stays
  in lockstep with the wizard's prompt logic.

The Cloudflare token is never written into the Caddyfile body — it is
substituted via `{env.CLOUDFLARE_API_TOKEN}` from the Caddy process's
environment. See [dns-providers.md](dns-providers.md) and
[security.md §secret hygiene](#secret-hygiene) for details.

> **Internal mode is plaintext HTTP.** The security-headers middleware
> suppresses HSTS, the Caddyfile drops every TLS block, and the
> scheduler skips the cert renewal handshake. The server must stay on
> a trusted private network (LAN / VPN / loopback). Operators who need
> encryption should switch to hostname mode (a real DNS name with
> ACME) or terminate TLS at a reverse proxy in front of the container.

## 9. Secret hygiene

The following principles are enforced throughout the codebase (AGENTS.md
§2.1):

- Passwords / tokens / DNS tokens / secret keys are **never logged**
- `__repr__` does not expose secrets (e.g. `CloudflareProvider.__repr__`
  shows only the zone)
- Exception messages do not include tokens (Cloudflare responses are
  masked via `re.sub(r"[A-Za-z0-9_-]{32,}", "***", ...)`)
- Messages surfaced as `ConfigError` are operator-friendly but never
  include credentials

`/etc/outo-models/config.yaml` is stored with **mode `0o600`**; the
wizard prints a warning about the permission.

## 10. Non-root container execution

The container runs as uid/gid 1000 (`app`) per the
[Containerfile](../Containerfile).

- Anything that requires host privileges — firewall, DNS, certificate
  renewal — is done by **host-side scripts** (`src/outo_models/assets/scripts/*.sh`),
  not from inside the container
- Caddy needs `--cap-add NET_BIND_SERVICE` to bind 80 / 443 as uid 1000; the
  `start` command attaches it automatically. Because the container runs
  host-networked (see below), that bind lands directly on the host's ports,
  which needs `net.ipv4.ip_unprivileged_port_start` lowered on the host (see
  the sysctl note below)
- The container shares the **host network namespace** (`--network=host`, no
  `-p` mappings) so the Spaces run proxy can reach the Space ports that
  rootless portmappings bind to the host loopback. That is acceptable because
  the server container is trusted code and already holds the Podman API
  socket; **Space** containers keep their own netnses, and their
  loopback-bound ports remain unreachable from any other container.
- After root-only steps (e.g. `pip install`), the entrypoint drops back to
  the non-root user immediately

### Sysctl: low ports on the host

Rootless Podman binds port 80 / 443 in the server container's (host) network
namespace through the HOST-side `rootlessport` helper, which the kernel only
permits when
`net.ipv4.ip_unprivileged_port_start` is low enough. Raising access to
low ports is a real privilege trade-off, so:

- Lowering the threshold lets **any local unprivileged user** bind host
  ports 80 and above. outo-models only ever writes `80` — never `0`,
  which would allow binding every port.
- The installer applies it **automatically only when the kernel is still
  at the default `1024`**. A host that already tuned the value by hand is
  left untouched.
- Operators can revert it:

  ```bash
  sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1024
  sudo rm /etc/sysctl.d/90-outo-models-low-ports.conf
  ```

  (Removing the drop-in stops the value from coming back on reboot.)
- The alternative — a hand-rolled launch publishing high host ports
  (`-p 8080:80 -p 8443:443`, without `--network=host`) — needs no sysctl
  change but makes the service unreachable on standard
  HTTPS and breaks ACME HTTP-01 (DNS-01 only). It also disables the Spaces
  `run/` proxy, whose target is the host loopback. See
  [troubleshooting.md §2](troubleshooting.md#2-port-80--443-binding-failures).

## 11. Host firewall boundary

The `outo-models` container never touches the host firewall directly.
Responsibility is split cleanly:

- In-container CLI: `outo_models.firewall.open_ports` builds the argv and
  runs `bash .../assets/scripts/firewall-open.sh <kind> <port...>`
- Host script: calls `firewall-cmd` / `ufw` / `nft` directly (with
  `set -euo pipefail`)
- When invoked non-root, `sudo -n` is attached automatically
- `sudo -n` failure surfaces as `OutoError(code="firewall_permission")`

See [architecture.md](architecture.md) and the firewall section of
[troubleshooting.md](troubleshooting.md) for the full flow.

## 12. LFS auth model

LFS requests reuse the **same Basic credentials** as regular git
smart-HTTP. There are no extra tokens or headers.

### Auth flow

[`git_smart/lfs.py`](../src/outo_models/git_smart/lfs.py) and
[`git_smart/auth.py`](../src/outo_models/git_smart/auth.py) work
together:

- `POST /info/lfs/objects/batch` — reads the body first. If the operation
  is `download`, public repos accept anonymous access; `upload` requires
  owner / admin.
- `PUT/GET /info/lfs/objects/{oid}` — the visibility matrix applies
  unchanged. Private repos are owner / admin only.

### `local` backend credential reuse

For the `local` backend, `LfsAction.href` is a **same-origin** URL
(`{base_url}/{owner}/{repo}.git/info/lfs/objects/{oid}`). `git-lfs` simply
re-sends the Basic credentials it used for the original clone/push, so no
extra headers are required — `LfsAction.headers` is empty.

Security implications of this model:

- Because Basic credentials reach the LFS PUT/GET endpoints, the
  endpoint **must be HTTPS** (the same "no security compromises" rule
  from AGENTS.md §2.1).
- The server doesn't trust the presence of `Authorization` blindly —
  `resolve_git_identity` + `authorize` re-validate on every request, just
  like a regular push/pull.
- `git-lfs`'s credential cache (e.g. `git config credential.helper store`)
  behaves the same as for regular pushes, so a user who hasn't explicitly
  cached their credentials is prompted every time.

### `s3` backend presigned URLs

For the `s3` backend, `LfsAction.href` is a presigned URL — a short-lived
SigV4 signature baked into the URL. The client does not add an
`Authorization` header (the signature itself plays that role).

Operational notes:

- `OUTO_S3_PRESIGN_TTL_SECONDS` (default 3600) — if it is too large, the
  presigned URL may linger in logs or caches before expiring. For
  environments that should not leak externally, keep it short
  (300–600 seconds).
- Presigned URLs expose the `s3_endpoint` origin (e.g.
  `https://s3.amazonaws.com/...` or `http://minio.local:9000/...`), so
  anyone who learns the URL can download within the TTL. For tighter
  access control, layer bucket policy / IAM on top (IP / VPC
  restrictions).
- Because presigned URLs bypass our server, neither `UserUsage` increment
  nor `add_usage` runs in the `s3` backend — the server has no way to
  know an object was uploaded. Audit logs are only emitted at the batch
  stage, not at PUT. Add a separate reconcile job if you need strict
  internal consistency.
- `presign_url` uses `outo_models.utils.time.utcnow()` for the SigV4
  `X-Amz-Date`. If the server clock drifts from UTC, S3 returns
  `SignatureDoesNotMatch` — see
  [troubleshooting.md §S3 presign clock skew](troubleshooting.md#s3-presign-clock-skew).

### S3 secret hygiene

`S3ObjectStore` in
[`objectstore/s3.py`](../src/outo_models/objectstore/s3.py) enforces:

- `__repr__` excludes `secret_key` (only endpoint / bucket / region /
  prefix are shown)
- `ConfigError` messages never include secrets
- Presigned URLs carry only the SigV4 signature, not the secret
- `S3ObjectStore(name="s3")` — `name` is a short tag for audit logs
  (`local` / `s3`)

> Inject secrets via the `OUTO_S3_SECRET_KEY` environment variable only.
> Do not write them directly into `/etc/outo-models/config.yaml` (a `0o600`
> warning is emitted, but plaintext secrets on disk should be avoided by
> design).

## 13. Spaces runtime isolation

`SpaceRuntimeManager` in
[`spaces/runtime_manager.py`](../src/outo_models/spaces/runtime_manager.py)
talks directly to the host's Podman. The instant a container is launched,
the host Podman daemon grants our container full container
create/delete rights — that is the trust boundary.

### Enforced isolation

| Dimension | Implementation |
| --- | --- |
| Non-root execution | our container runs as uid/gid 1000 (AGENTS.md §4) |
| Container port fixed | only `8000/tcp` exposed; host bind IP is `127.0.0.1` — no external exposure |
| Host port pool | assignments only from `OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` (default 20000..21000) |
| Label-based management | `outo.managed=true` + `outo.space=<owner>/<name>` — does not collide with other containers on the host |
| Container name rule | `outo-space-<owner>-<name>` — name collisions immediately raise `ConflictError` |
| Image tag rule | `localhost/outo-space-<owner>-<name>:latest` — isolated from other namespaces |

### The Podman socket mount is the trust boundary

Mounting the host's Podman API socket
(`/run/podman/podman.sock` or a rootless socket at
`/run/user/<uid>/podman/podman.sock`) into the container delegates
container create/delete rights over the entire host. With a rootless socket
that is host-root-equivalent inside the invoking user's namespace.

`outo-models start` does this mount automatically, so a default install
already carries it. The host path is taken from `OUTO_PODMAN_SOCK` (set by
the CLI shim) or probed rootless-first; the in-container path is
`OUTO_PODMAN_SOCKET` (default `/run/podman/podman.sock`). Deployments that
launch the container themselves — quadlet, systemd units, a bare
`podman run` — must mount it themselves:

```bash
# rootless Podman (user 1000) user socket
-v /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro
-e OUTO_PODMAN_SOCKET=/run/podman/podman.sock
```

`:ro` is **not** a security control for a Unix socket — the read-only flag
governs the file system, not socket permissions, and Podman would refuse the
write operations anyway. Keep it for tidiness, not for isolation.

`outo-models start` also mounts the host CDI spec directories
(`/etc/cdi`, `/var/run/cdi`) read-only when they exist. The server reads
them only to build the device/mount/env list for Podman versions older than
6.0 (see [spaces.md § Podman version compatibility](spaces.md#podman-version-compatibility));
the contents are declarative device definitions, never executed.

Additional recommendations:

- On the host, run `podman system connection ls` to verify the user socket
  permissions, and lock them to `0660` with the `podman` group.
- In the systemd unit, grant only our container access to the user socket
  via `SupplementaryGroups=podman`.
- Networking: host Podman's container network defaults to `bridge`, but
  you should add host-side `iptables` / `nftables` rules so external
  traffic cannot reach the container's `8000/tcp` directly.

### Scoped framing relaxation on the Space run proxy

Every page served by `outo-models` sends `X-Frame-Options: DENY` and a CSP
whose `frame-ancestors 'none'` forbids framing. There is exactly one scoped
exception: the Space run proxy path `/spaces/{owner}/{name}/run/**`.

Responses under that path send `X-Frame-Options: SAMEORIGIN` and a CSP
containing only:

```
frame-ancestors 'self'
```

No `default-src`, no `script-src`. The document behind that path is
user-supplied application content rendered by the user's own container, and
its scripts have to execute; a site-wide script policy would break every
Space. `frame-ancestors 'self'` still blocks any third-party site from
framing a Space.

This is deliberate: the code in a Space container is already arbitrary,
trusted-at-that-level user code that the owner chose to run. The relaxation
adds no new capability, it only stops the site-wide framing policy from
crippling user apps. Upstream framing headers (`X-Frame-Options`,
`Content-Security-Policy`, and the other hop-by-hop headers) are stripped
from the proxied response before it reaches the browser, so the container's
own policies cannot be used to loosen ours and ours cannot be used to
tighten theirs.

### `docker` SDK Dockerfile / Containerfile enforcement

The `docker` SDK Space is rejected with `ValidationFailedError` during
`start` / `restart` if the repo root is missing a `Dockerfile` or
`Containerfile` — this prevents a container from accidentally being
built off a public base image like `python:3.12`. See the
`_run_lifecycle` branch in
[`spaces.py:start_space`](../../src/outo_models/server/routers/spaces.py).

### GPU CDI prerequisites

GPUs assigned via `outo-models admin gpu assign <name> <ids...>` are
attached as `nvidia.com/gpu=<id>` CDI devices. The host must have:

- `nvidia-container-toolkit` installed and the CDI specification enabled
  (`/etc/cdi/nvidia.yaml`)
- `podman run --device nvidia.com/gpu=0 ...` working (manual check)

If any prerequisite is missing, `start` fails with
`OutoError(code="podman_api", status_code=502)` — see the Podman section
of [troubleshooting.md](troubleshooting.md).

## 14. `reset` safety gate

[src/outo_models/cli/reset.py](../src/outo_models/cli/reset.py) enforces
AGENTS.md §2.2 in code.

- Without `--destroy`, the command is **always a dry-run** (no deletion)
- Without `OUTO_DESTRUCTIVE=1`, `--destroy` is refused
- Only when both are present do the three `yes` prompts run
- Answers must be exactly `yes` (case-sensitive; whitespace, `y`, blank
  lines are all rejected)
- A single wrong answer aborts immediately and exits 1
- EOF aborts safely

**Any PR that weakens this safety guard is rejected.** There is no bypass
path.

## Next steps

- [admin.md](admin.md) — admin PAT issuance / revocation operations
- [git-repos.md](git-repos.md) — credential flow during git clone / push
- [troubleshooting.md](troubleshooting.md) — debugging secret / auth
  problems
