# Troubleshooting

Common operational situations, collected. Message sources are in the code
itself, so when tracking down the cause consult the corresponding module
in [src/outo_models](../src/outo_models).

## 1. Firewall

### "No firewall detected"

`firewalld` / `ufw` / `nft` are not installed on the host.
`firewall.open_ports.detect_firewall` returns `FirewallKind.NONE`, and
`firewall-open.sh` prints English guidance to stdout.

```
No firewall detected. outo-models requires you to open the externally
reachable ports (80, 443) directly through the OS firewall or the cloud
security group.
```

Open ports 80 / 443 yourself. In `setup`, pass `--skip-firewall` to skip
the firewall step and add the inbound rule through your production /
cloud console.

### "Insufficient privilege for the firewall command"

`outo-models setup` (or an equivalent `open_ports` call) fails with
`sudo -n` while invoking the host script
(`OutoError(code="firewall_permission")`). The wizard converts this to a
`ConfigError` written to stderr.

Resolutions:

1. Re-run `setup` as `root`, or
2. Add a NOPASSWD rule in `/etc/sudoers.d/outo-models`:

```
<your-username> ALL=(root) NOPASSWD: /opt/outo-models/scripts/firewall-open.sh *
```

`firewall-open.sh` runs with `set -euo pipefail` and only executes the
`<kind> <port...>` arguments it receives, so this is safe.

### nftables rule conflict

`firewall-open.sh` accumulates rules in its own `inet outo_models`
table (skipping duplicates). Sharing that table with another tool causes
conflicts. Clean it up with:

```bash
sudo nft delete table inet outo_models
```

## 2. Port 80 / 443 binding failures

Binding 80 / 443 from a non-root process requires the `NET_BIND_SERVICE`
capability. The entrypoint warns ahead of time when run inside the
container
([container/rootfs/usr/local/bin/outo-entrypoint.sh](../container/rootfs/usr/local/bin/outo-entrypoint.sh)):

```
[warn] container is running as a non-root user (uid=1000) and the kernel
       does not permit unprivileged binds to ports below 80 (i.e.
       net.ipv4.ip_unprivileged_port_start > 80). Caddy is likely to fail
       with a permission error on startup.

       Fix one of the following:
         1) podman run --cap-add NET_BIND_SERVICE ...   # recommended
         2) host port remap: -p 8080:80 -p 8443:443  # TLS termination must be handled elsewhere
```

### Recommended fix

`outo-models start` always attaches `--cap-add NET_BIND_SERVICE`, so
most installs are fixed automatically. Add the same option when launching
the container by hand.

### Host sysctl tuning

If `/proc/sys/net/ipv4/ip_unprivileged_port_start` is `0`, unprivileged
processes can bind to ports below 80 — but this is **not recommended**
from a security standpoint.

```bash
# Temporary
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0

# Persistent
echo 'net.ipv4.ip_unprivileged_port_start=0' | sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
```

### Host port remapping

Mapping 80 / 443 to other ports makes the service unreachable over
standard HTTPS from the public internet. **Avoid this outside of test /
demo setups.** Example:

```bash
-p 8080:80 -p 8443:443
```

In this case Caddy can't receive the ACME HTTP-01 challenge on 8080, so
only DNS-01 (Cloudflare mode) is viable.

## 4. ACME certificate issuance / renewal

### "Let's Encrypt rate limit reached"

Repeated issuance attempts against a typo'd domain will trigger a Let's
Encrypt rate limit. Switch to the staging CA temporarily to debug:

Render the Caddyfile with `TlsConfig.staging = True`. The current code
does not expose a direct staging toggle flag from `outo-models setup`,
so add the line
`acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` to
`/etc/outo-models/Caddyfile` and restart Caddy.

### DNS-01 challenge failure

In Cloudflare mode, missing or insufficient `CLOUDFLARE_API_TOKEN`
permissions cause Caddy to emit token-related errors on stderr. Verify
the token meets all of:

- `Zone.DNS:Edit` permission
- Token's `Zone Resources` is restricted to the right zone (not all
  zones)
- Not expired

### 30-day expiry warning

`cert_renewal_job` checks certificates daily at 00:00 UTC. If anything
is unhealthy it nudges Caddy with a reload. If renewal still fails:

1. Inspect `journalctl -u outo-models` for the Caddy log
2. Run `curl -v https://<domain>/` to confirm external reachability
3. Check the Caddy version with `podman exec outo-models caddy version`

## 5. "unable to open database file"

The container's non-root user (uid 1000) lacks read / write permission on
the directory containing the DB file. Check:

```bash
ls -ld /var/lib/outo-models
# drwxr-xr-x 1000 1000 ...
```

If the owner is wrong (e.g. another container used the directory
earlier):

```bash
sudo chown -R 1000:1000 /var/lib/outo-models
```

The directory created by `setup` ships with the right permissions, but
host-side tooling can change them.

## 6. podman missing (development machine)

The development machine (AGENTS.md §4) does not have podman. `start` /
`stop` / `restart` / `update` / `reset` print an English message and exit
1.

```
error (config_error): this command must run on the server host (podman not installed).
Re-run it from the host of the container deployment.
```

`status` is the exception — it exits 0 with the following:

```
[info] podman is not installed on this host (development environment).
```

In the development environment we only guarantee `uv sync` + `make lint`
+ `make typecheck` + `make test` (see [testing.md](testing.md)). Verify
the image's runtime behavior on a separate test machine.

## 7. Log locations

- **Container logs**: `podman logs outo-models` (stdout/stderr unified)
- **Host-side script logs**: `firewall-open.sh` writes to stdout;
  `update.sh` / `reset.sh` write to stdout — pass
  `podman run --log-driver journald ...` to ship them to journald
- **Caddy access log**: stored alongside `/var/lib/outo-models/certs/` in
  Caddy's internal log. See `podman exec outo-models caddy fmt --help`
  for details.
- **DB audit log**: `audit_logs` table. `outo-models admin list` does not
  show it; query through `GET /api/admin/audit` (see
  [admin.md#inspecting-audit-logs](admin.md#inspecting-audit-logs)).

## 8. Container exits immediately after start

If the container exits right after `podman run`, check:

```bash
podman logs outo-models
```

Common causes:

1. **Entrypoint rejects the dev+production combination** — starting with
   `IMAGE_FLAVOR=dev` + `OUTO_ENV=production` exits 1. Switch to the
   `stable` image or set `OUTO_ENV=development`.
2. **Missing `outo-models` console script** — the venv was corrupted
   during build. Check with
   `podman exec outo-models which outo-models`.
3. **`/etc/outo-models/config.yaml` is malformed** — re-run `setup`
   (idempotent regeneration).
4. **Port 80 / 443 conflict** — another web server (nginx / apache) is
   already bound on the host. Check with
   `sudo ss -lntp | grep -E ':80|:443'`.

## 9. git operations start returning 401

- Check whether the PAT has expired (`GET /api/auth/tokens`, look for
  `expires_at`).
- Check whether the PAT was revoked (the operator revoked it explicitly,
  or the user pressed the revoke button in the UI).
- Check whether the user is `banned` or `denied`. In that case the
  expected response is 403, but with Basic auth a 401 is also possible —
  `git_smart.auth` raises `ForbiddenError` when `user.is_active == False`,
  though the upstream router may convert it to a 401 from `authorize`.

## 10. The `reset` gate activates by accident

The three-`yes` gate (AGENTS.md §2.2) only proceeds when **all** of the
following hold:

- `--destroy` is present on the CLI
- `OUTO_DESTRUCTIVE=1` is set in the environment
- Three exact `yes` answers in a row (case-sensitive; whitespace,
  different answers, blank lines all reject)

Bypassing in a script only requires `--destroy` plus the env var. The
reset cannot be automated unattended — that is by design.

If "data that should not have been touched is already gone," it is gone
(no snapshot / backup policy). For future operations we recommend a
backup policy such as:

- Daily off-host backup of `data_dir` (`podman exec outo-models sqlite3
  /var/lib/outo-models/db.sqlite3 ".backup /backup/db-$(date +%F).sqlite3"`)
- `data_dir/repos/` is a set of git repos, so `git clone --mirror`
  produces an external mirror

## 11. LFS error responses

Every response code for `/info/lfs/objects/batch` and
`/info/lfs/objects/{oid}` is tabulated in
[git-repos.md §Error codes](git-repos.md#error-codes). This section only
covers debugging the most common cases.

### 406 / 415 — missing `Accept` or `Content-Type`

The request is missing `Accept: application/vnd.git-lfs+json`, or the
body's `Content-Type` is wrong. The server runs its own ASGI handler
independent of fastapi, so non-LFS clients (e.g. an old `git-lfs`)
trigger this because they don't conform to the LFS spec.

Fix: re-install the LFS client-side filter with `git lfs install` and
verify `filter.lfs.*` is set with `git config --list | grep lfs`.

### 413 — object too large or quota exceeded

The batch response body or the PUT response body is one of:

```json
{ "error": "object size 2147483648 exceeds per-object limit 5368709120" }
```

→ Exceeded `OUTO_LFS_MAX_OBJECT_BYTES` (default 5 GiB). Raise
`lfs_max_object_bytes` in `/etc/outo-models/config.yaml` or split the
object.

```json
{ "error": "quota exceeded: used=… + incoming=… > max=…" }
```

→ User quota exceeded. Check `used` / `max` with
`outo-models admin quota show <name>` and raise it with
`quota set <name> 50GiB`.

The batch response expresses these as **per-object** errors (one 413 does
not fail the other objects). After the batch, the actual upload can still
hit 413 — in that case the user has to shrink the LFS commit.

### 422 — batch JSON format error

- `operation` is not `"upload"` / `"download"`
- `oid` is not 64-char hex (e.g. sha256 not used, wrong length)
- `transfers` does not include `"basic"`

The server replies per the LFS spec with `application/vnd.git-lfs+json`.
Inspect the client log and verify the tracked oids with `git lfs
ls-files`.

### S3 presign clock skew

When `OUTO_LFS_BACKEND=s3`, `git lfs push` fails with:

```
S3 returned SignatureDoesNotMatch (or RequestTimeTooSkewed)
```

Cause: the SigV4 signature is generated from `X-Amz-Date`. When the
server / client / S3 clocks drift by more than 5 minutes, the request is
rejected.

Resolution:

```bash
# 1) Check how far the server clock has drifted from UTC
date -u; date

# 2) Verify chronyd / systemd-timesyncd is alive
systemctl status chronyd   # or systemd-timesyncd

# 3) Check the in-container clock (Podman shares it by default, but some
#    environments separate it)
podman exec outo-models date -u
```

If the server clock is fine, the issue is on the client or S3 side. The
most common cause is **RTC drift on a long-running server**, so verify
with `chronyc tracking`.

### Presigned URLs leaking externally

If `OUTO_S3_PRESIGN_TTL_SECONDS` is too large, the presigned URL may
linger in logs / caches before expiring. The default is 3600 seconds, but
for environments that should not leak externally we recommend 300–600
seconds (see [security.md §`s3` backend presigned URLs](security.md#s3-backend-presigned-urls)).

## 12. Podman / Spaces runtime errors

### 503 `runtime_disabled`

The response from `POST /api/spaces/<owner>/<name>/start` (or `/stop`,
`/restart`, `/run/...`) is:

```json
{ "error": "runtime_disabled",
  "message": "Runtime is disabled. Ask the administrator to set OUTO_SPACES_RUNTIME_ENABLED=true and retry." }
```

Cause: the container is running with `OUTO_SPACES_RUNTIME_ENABLED=false`
(the default).

Resolution:

```bash
podman inspect outo-models --format '{{.Config.Env}}' | tr ',' '\n' | grep OUTO_
# If OUTO_SPACES_RUNTIME_ENABLED is missing or false, inject it and restart
sudo podman stop outo-models
sudo podman rm outo-models
sudo outo-models start
# or set spaces_runtime_enabled: true in config.yaml
```

### 503 `podman_unreachable`

```json
{ "error": "podman_unreachable",
  "message": "Cannot connect to the Podman socket. Check that the host's /run/podman/podman.sock file is mounted into the container." }
```

Cause: the Unix socket pointed to by `OUTO_PODMAN_SOCKET` is not
reachable inside the container. The most common reasons are a missing
mount, or confusion between rootless and system mode.

Resolution steps:

```bash
# 1) Verify the host Podman socket actually exists
ls -l /run/podman/podman.sock 2>/dev/null \
  || ls -l /run/user/$(id -u)/podman/podman.sock 2>/dev/null

# 2) Verify the same path is visible inside the container
podman exec outo-models ls -l /run/podman/podman.sock

# 3) If the mount is missing, add it to the start command
sudo podman run -d --name outo-models \
  -v /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro \
  ...

# 4) Probe REST directly inside the container
podman exec outo-models \
  curl --unix-socket /run/podman/podman.sock \
  http://d/v4.0.0/libpod/containers/json?all=true
```

### 502 `space_build_failed`

```json
{ "error": "space_build_failed",
  "message": "Image build failed: …Podman API returned an error: <last 2 KiB>" }
```

Cause: Podman's `POST /libpod/build` returned 4xx / 5xx. The tail of the
message is the last 2 KiB of the Podman build log (`_build_failure_tail`)
— Dockerfile / Containerfile errors surface there verbatim.

Resolution steps:

1. For `docker_sdk` / `gradio` / `streamlit` SDKs, confirm the repo root
   contains a `Dockerfile` or `Containerfile`
2. Reproduce the build directly inside the container for the full log:
   ```bash
   podman exec outo-models \
     podman build --build-context /tmp/site=<(git clone <repo> /tmp/site) -
   ```
3. Confirm the base image (`FROM`) is pullable from inside the container —
   in air-gapped environments, `podman pull` it ahead of time

### 503 `space_not_running`

When accessing `/spaces/<owner>/<name>/run/`:

```json
{ "error": "space_not_running",
  "message": "Space is not running. Start it and try again." }
```

Cause: container exited / stopped / was never created.

Resolution:

```bash
curl -b cookies.txt https://<domain>/api/spaces/<owner>/<name>/status
# If state is stopped / failed, start it
curl -X POST -b cookies.txt https://<domain>/api/spaces/<owner>/<name>/start
```

### 503 "all runtime ports are in use"

Cause: the 1000 (or operator-configured) ports in
`OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` are all taken.

Resolution:

1. List Spaces with
   `curl -b cookies.txt https://<domain>/api/spaces` and `stop` or
   `delete` any unused containers
2. In production, raise `OUTO_SPACES_RUNTIME_PORT_RANGE_END` and restart
   the container

### GPU CDI errors

`start` returns:

```
Podman API returned an error: … nvidia.com/gpu=0: no such device …
```

Resolution:

```bash
# 1) Confirm the host has a CDI specification
ls /etc/cdi/nvidia.yaml

# 2) Try the device directly with podman
podman run --rm --device nvidia.com/gpu=0 docker.io/library/cuda-vectoradd:nvidia-12.0.0
# If this fails, it's a host setup issue — update nvidia-container-toolkit
# and the CDI specification
```

## Next steps

- [install.md](install.md) — first-time install steps
- [setup-wizard.md](setup-wizard.md) — exact behavior of each automated
  step
- [security.md](security.md) — authentication / token / audit log policy
