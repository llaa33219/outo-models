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

### "firewall_container_host_required" — running via the host shim

When the wizard runs **inside a container** (the normal case for the
host-shim install — see [install.md](install.md)), it cannot reach the
host's firewall tools. The shim detects this and raises
`OutoError(code="firewall_container_host_required")` whose message
includes the host command the operator must run:

```bash
# The message looks like:
# "firewall tool not available in container; run
#  /usr/local/share/outo-models/firewall-open.sh firewalld 80 443 on the host"
sudo /usr/local/share/outo-models/firewall-open.sh firewalld 80 443
# or for ufw:
sudo /usr/local/share/outo-models/firewall-open.sh ufw 80 443
# or for nftables:
sudo /usr/local/share/outo-models/firewall-open.sh nftables 80 443
```

The script self-elevates with `sudo -n` so the operator does not need to
re-run it as root. The wizard prints a yellow warning with the host
command and **continues** — the install still completes; the firewall
rule is the operator's responsibility.

In **internal mode** (no ACME / no public hostname), the firewall step
is intentionally tolerant for exactly this reason: the install can
finish without firewall ports being touched from inside the container.

## 2. Port 80 / 443 binding failures

Getting 80 / 443 onto the wire involves **two independent binds**, and only
one of them is fixable from inside the container:

1. **Inside the container network namespace** — Caddy (running as uid 1000)
   binds `:80` and `:443`. This is what `--cap-add NET_BIND_SERVICE` solves.
2. **On the host** — rootless Podman publishes the container port through
   the HOST-side `rootlessport` helper, and *that* helper is subject to
   `net.ipv4.ip_unprivileged_port_start`. `--cap-add NET_BIND_SERVICE`
   does **not** help here; the capability only applies inside the
   container.

Layer 2 is what actually breaks on a default rootless host, and it is what
the rest of this section is about.

### Layer 1 — in-container bind

`outo-models start` always attaches `--cap-add NET_BIND_SERVICE`, so this
layer is fixed automatically. Add the same option when launching the
container by hand:

```bash
podman run --cap-add NET_BIND_SERVICE ...
```

The entrypoint prints a warning when it detects that the container-side
bind may be refused
([container/rootfs/usr/local/bin/outo-entrypoint.sh](../container/rootfs/usr/local/bin/outo-entrypoint.sh)).
Note that this warning is a heuristic about the *container* namespace and
can still fire on a fresh netns even when the host is configured
correctly — a known false positive. Judge by whether the server actually
comes up, not by the warning alone.

### Layer 2 — host publication (rootless Podman)

`outo-models start` preflights this **before touching any container** —
before it removes a stopped container and before it runs `podman run`. If
rootless Podman is in use *and* a configured port sits below the kernel's
`net.ipv4.ip_unprivileged_port_start`, it aborts with exit code 1 and the
error code `low_ports_blocked`:

```
podman is rootless and the host only allows unprivileged binds from net.ipv4.ip_unprivileged_port_start, so port 80 cannot be published: the host-side rootlessport helper has to bind it. Enable low ports on the host (the script asks for sudo itself):

    /usr/local/share/outo-models/enable-low-ports.sh 80
```

The detection **fails open**: if `podman info` errors, times out, or
returns something unexpected, `start` proceeds rather than blocking you
on a detection glitch. No new flags or environment variables are involved.

If `podman run` itself fails, `start` echoes podman's own captured output
verbatim, then `[error] container failed to start (exit=N)`, then a
diagnosis block. A `rootlessport cannot expose privileged port` signature
matches this rule:

```
Podman could not publish the low port: the HOST-side rootlessport helper has
to bind it, and the kernel only lets unprivileged binds start at
net.ipv4.ip_unprivileged_port_start (1024). Note that `--cap-add
NET_BIND_SERVICE` applies INSIDE the container — it cannot help the host-side
rootlessport helper, which is the one failing here. Fix it once on the host:

    /usr/local/share/outo-models/enable-low-ports.sh 80

(it asks for sudo and applies a persistent sysctl). Then: outo-models start
```

### Automatic remediation at install time

`scripts/install-cli.sh` reads
`/proc/sys/net/ipv4/ip_unprivileged_port_start` after installing the host
helper scripts. **Only when the value is exactly the kernel default
`1024`** does it run
`bash /usr/local/share/outo-models/enable-low-ports.sh 80`. A host that
already lowered the threshold by hand is never touched again. Its output
tells you which path was taken:

- threshold `1024`, applied:
  `[ok] net.ipv4.ip_unprivileged_port_start=80 applied on the host (persistent)`
- threshold `1024`, failed:
  `[warn] could not apply the low-port sysctl; ports 80/443 may fail to publish.`
  followed by `       run it manually: sudo bash /usr/local/share/outo-models/enable-low-ports.sh 80`
- threshold already ≤ 80:
  `[ok] unprivileged low ports already allowed (threshold <value>)`
- any other non-empty value (e.g. `1023`) — left as-is:
  `[note] net.ipv4.ip_unprivileged_port_start is <value> (not the default 1024) — left as-is.`
  followed by `       to lower it: sudo bash /usr/local/share/outo-models/enable-low-ports.sh 80`

An empty read (no procfs) is silent.

### Manual fix

If the installer skipped or failed, run the helper once by hand. It asks
for sudo itself and writes a persistent
`/etc/sysctl.d/90-outo-models-low-ports.conf` drop-in:

```bash
/usr/local/share/outo-models/enable-low-ports.sh 80
```

The `outo-models` shim itself runs rootless (`--userns=keep-id`), so do
**not** prefix CLI invocations with `sudo` — the elevated step is the host
helper script, nothing else.

To do it fully manually instead (the value should be your lowest exposed
port — `80` in a typical install; `0` allows binding every port and is not
recommended):

```bash
# Temporary
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80

# Persistent
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
```

### Quadlet users

The [quadlet example](../container/examples/quadlet/outo-models.container)
bypasses `install-cli.sh` entirely — nothing installs the helper and
nothing applies the sysctl for you. A rootless quadlet deployment must run
the host command once itself:

```bash
/usr/local/share/outo-models/enable-low-ports.sh 80
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

### Internal-mode installs skip renewal entirely

`cert_renewal_job` short-circuits when `Settings.is_internal` is True
(the domain is an IP literal / empty) — there is no cert to renew in
plain-HTTP mode. The scheduler still ticks, but no `:443` handshake is
attempted; the IP may not even have a `:443` listener. If you see
"certificate unhealthy" log lines on an internal-mode install, the
wizard was misconfigured — re-run it with `--skip-dns` cleared and a
proper `--domain` (hostname) so ACME issuance can take over.

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

### The same "podman not installed" error appears through the shim

Through the shim the CLI runs inside a throwaway container that reaches the
host's podman via the API socket (`podman-remote`). This error there means
the socket is missing. Fix on the host:

```bash
systemctl --user enable --now podman.socket   # rootless installs
sudo systemctl enable --now podman.socket     # rootful installs
```

The installer does this best-effort for you; reinstalling the shim
(`install-cli.sh`) retries it. Rootless is the supported path for the
*server* container: it runs as uid 1000 and gets `EACCES` on a rootful
`root:root 0660` socket. See
[503 `podman_unreachable`](#503-podman_unreachable).

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

`outo-models start` detects this case itself: it exits 1 with
`start_verify_failed` and prints the last 50 log lines (skip verification
with `--no-verify`). To check by hand:

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
{ "error": "object exceeds per-object limit 107374182400 bytes (raise OUTO_LFS_MAX_OBJECT_BYTES ...)" }
```

→ Exceeded `OUTO_LFS_MAX_OBJECT_BYTES` (default 100 GiB). Raise
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

Cause: the container is running with `OUTO_SPACES_RUNTIME_ENABLED=false`.
Set it to `true` (the default) and restart, or fix the operator's intent.

Resolution:

```bash
podman inspect outo-models --format '{{.Config.Env}}' | tr ',' '\n' | grep OUTO_
# If OUTO_SPACES_RUNTIME_ENABLED is missing or false, inject it and restart
sudo podman stop outo-models
sudo podman rm outo-models
outo-models start
# or set spaces_runtime_enabled: true in config.yaml
```

### 503 `podman_unreachable`

```json
{ "error": "podman_unreachable",
  "message": "Cannot connect to the Podman socket. `outo-models start` mounts it automatically; quadlet and other deployments must mount the host socket themselves (OUTO_PODMAN_SOCKET, default /run/podman/podman.sock)." }
```

Cause: the Unix socket pointed to by `OUTO_PODMAN_SOCKET` is not reachable
inside the container. `outo-models start` mounts the host socket for you
(`-v <host-socket>:/run/podman/podman.sock:ro` plus
`-e OUTO_PODMAN_SOCKET=/run/podman/podman.sock`), so on the standard path
this means one of two things: the host socket was not found at start time,
or only a rootful one existed. A deployment that launches the container
itself (quadlet, a bare `podman run`) bypasses the auto-mount entirely.

**Rule of thumb: rootless first.** The server container runs as uid 1000
and cannot connect to a rootful `root:root 0660` socket — that is an
`EACCES`, not a missing file. Enable the rootless user socket as the
non-root user that runs `outo-models`:

```bash
systemctl --user enable --now podman.socket
```

Resolution steps:

```bash
# 1) Verify the host Podman socket actually exists (rootless first)
ls -l "$(echo "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}")/podman/podman.sock" 2>/dev/null \
  || ls -l /run/podman/podman.sock 2>/dev/null

# 2) Verify the same path is visible inside the container
podman exec outo-models ls -l /run/podman/podman.sock

# 3a) Standard path: restart so `start` re-probes and mounts the socket
outo-models start
# If it printed
#   [note] no Podman socket found — Spaces container SDKs (docker/gradio/streamlit) stay unavailable until a Podman socket is mounted.
# the host had no socket at start time. Fix (1), then start again.

# 3b) Quadlet / manual deployment: mount it yourself
#   Volume=%t/podman/podman.sock:/run/podman/podman.sock:ro
#   Environment=OUTO_PODMAN_SOCKET=/run/podman/podman.sock
# and restart the unit. Override the host path start should mount with
# OUTO_PODMAN_SOCK=/run/user/<uid>/podman/podman.sock outo-models start

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

### Podman: `unresolvable CDI devices`

```
Podman API returned an error: … setting up CDI devices: unresolvable CDI devices nvidia.com/gpu=0
```

Podman's CDI registry matches device names **exactly**: `nvidia.com/gpu=0`
resolves only if a spec of kind `nvidia.com/gpu` literally defines a device
called `0`. Specs generated by `nvidia-ctk cdi generate` on newer
nvidia-container-toolkit releases name their devices `gpu-0` instead, so the
same GPU is unresolvable under the bare index.

The server reads the specs before the create and sends the name the spec
defines, so this error does not occur for a resolvable assignment — the
assignment is canonicalized, and `gpu-0`, `0`, `nvidia.com/gpu=gpu-0` and
`GPU-<uuid>` are all accepted spellings of the same device. If you hit it
anyway, the create body carried a name no spec defines: check what the spec
calls the device.

```bash
podman exec outo-models grep -A5 '^devices:' /etc/cdi/nvidia.yaml
podman run --rm --device nvidia.com/gpu=gpu-0 docker.io/library/cuda-vectoradd:nvidia-12.0.0
```

**A second, subtler cause: an older Podman CDI parser drops a newer spec.**
Podman 4.x ships an older `containers/common`, whose CDI spec parser does not
understand a newer `cdiVersion` — a spec written as `cdiVersion: 0.7.0`, for
instance. Podman then reports *no spec at all*, even though the file is
present, readable and defines exactly the device you assigned:

```bash
# The spec is there, and it declares the device:
podman exec outo-models grep -E '^(cdiVersion|kind):' /etc/cdi/nvidia.yaml
podman exec outo-models grep -A5 '^devices:' /etc/cdi/nvidia.yaml
# …but podman's own registry cannot see it:
podman run --rm --device nvidia.com/gpu=0 docker.io/library/cuda-vectoradd:nvidia-12.0.0
```

You do not have to fix the host. When a container with an assigned GPU is
refused, the server translates the spec edits itself and retries the
create-and-start once — `devices` (device nodes), `mounts` and `env` merged
into the create body — so the GPU arrives on that combination too. Podman
resolves CDI at *start*, not at create, so on these hosts the create often
succeeds and the start answers
`setting up CDI devices: unresolvable CDI devices nvidia.com/gpu=0`; the
server treats both refusals the same way. If the retry is refused as well, you
get the `space_gpu_cdi_unresolved` diagnosis below with Podman's message
appended.

The translation reproduces the hook effects a create body *can* express, so a
Space gets a working `nvidia-smi` without any manual step: each
`create-symlinks --link <target>::<link>` pair becomes a read-only bind mount
of `<target>` at `<link>` (this is what provides the SONAME symlink
`libnvidia-ml.so.1`, which the dynamic loader asks for by name), and every
`update-ldcache --folder` is prepended to `LD_LIBRARY_PATH` (this is what
registers the injected library folders with the loader in place of running
`ldconfig` inside the container). A hook with no create-body spelling —
`enable-cuda-compat`, `chmod`, `update-application-profile`, or a malformed
`--link` pair — cannot be translated; it produces a delivery note naming the
hook **command**, shown in the Space's runtime message
(`The Space is running. CDI hooks dropped for 0 (enable-cuda-compat): …`) and
logged at `WARNING` on the server. Hooks never fail the delivery. When
Podman's own CDI path works, none of this applies.

If `nvidia-smi` still reports `NVIDIA-SMI couldn't find libnvidia-ml.so
library` after this translation, compare the spec's SONAME symlink target with
the library version the spec actually mounts — the two must match, and a spec
generated for a different driver version will not:

```bash
podman exec outo-models grep -A6 'create-symlinks' /etc/cdi/nvidia.yaml
podman exec outo-models grep -E 'libnvidia-ml' /etc/cdi/nvidia.yaml
```

### The container starts but has no GPU

Symptom: the Space runs, the GPU is assigned in the admin UI, but inside the
container there is no `/dev/nvidia*`, no `NVIDIA_*` environment, and
`nvidia-smi` is missing.

This is what Podman < 6.0 does with a `gpus` field: `specgen.SpecGenerator`
has no such key before 6.0 and Go's JSON decoder **silently ignores unknown
fields**, so the request succeeds and the GPU is simply dropped. The server
detects this (`GET /libpod/version`) and sends CDI qualified device names in
`devices` on older releases, which Podman's own CDI path resolves.

So a GPU-less container on old Podman means the CDI specs were not visible to
the server:

```bash
# 1) Does the host have a spec at all?
ls /etc/cdi/nvidia.yaml /var/run/cdi/nvidia.yaml

# 2) Is it mounted into the server container?
podman exec outo-models ls /etc/cdi

# 3) Is the device defined by the spec?
podman exec outo-models grep -A2 '^devices:' /etc/cdi/nvidia.yaml
```

A self-managed deployment (quadlet, a bare `podman run`) has to mount them:

```bash
-v /etc/cdi:/etc/cdi:ro -v /var/run/cdi:/var/run/cdi:ro
```

Through the standard CLI shim, `outo-models start` does this automatically:
the shim runs on the host, probes `/etc/cdi` and `/var/run/cdi`, and forwards
the existing ones as `OUTO_CDI_DIRS` (`start` runs inside a container and
cannot probe the host itself). If step 2 above shows an empty `/etc/cdi`, the
shim predates that forwarding — refresh it and recreate the container:

```bash
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash
outo-models update
```

Non-default spec locations go through the same channel: `OUTO_CDI_DIRS`
(a colon-separated list of host directories) overrides the probe, e.g.
`OUTO_CDI_DIRS=/etc/cdi:/opt/cdi outo-models start`.

### 422 `space_gpu_cdi_unresolved`

```
CDI device '0' of kind 'nvidia.com/gpu' is not defined in the specs found in
/etc/cdi, /var/run/cdi — defined devices: 0.
Podman refused the container:
Podman API returned an error: no such device nvidia.com/gpu=0
```

Podman resolves the CDI device names the create body carries (see
[spaces.md § Podman version compatibility](spaces.md#podman-version-compatibility)),
and the server reads the same specs itself first. This error therefore comes
in two shapes: raised *before* the create when the specs cannot resolve the
assignment at all, and raised *after* a Podman refusal that the server's own
re-resolution could not explain either — in which case the message carries
Podman's refusal text as well. Either the spec file is missing / not mounted
and also unreadable on the host, the device name in the assignment has no
matching entry in the spec's `devices:` block, or the spec requests an edit
the resolver cannot account for — the message names which one. The translated
fallback expresses `deviceNodes`, `mounts`, `env` and `additionalGids` (as the
container's `groups`), so only a genuinely unknown key (intel-rdt, net
devices, a typo such as `additionalGIDs`) or a malformed value inside a known
key reaches this error. Regenerate the spec without that edit, or assign
a device the spec defines. OCI hooks are *not* a cause: Podman applies them
itself on its own path, and on the translated path their two expressible
effects (symlink mounts, `LD_LIBRARY_PATH`) are delivered — the server only
records the commands it could not translate, and it never refuses a spec for
them.

When both resolution sources failed, the message ends with a line naming both
attempts (`Both resolution paths were attempted: …`): the server
container's own filesystem, then the same directories read on the host
through the helper container. That line rules out the mount plumbing and
leaves exactly two causes: no specs on the host at all, or an unknown GPU
id.

The "no spec of this kind" variant is the case where the server container
cannot see valid host specs at all. It reports, one line per searched
directory, what the resolver actually observed:

```
No CDI spec of kind 'nvidia.com/gpu' found. What the resolver actually saw:
  /etc/cdi: present, 1 readable spec (1 unreadable), kinds: amd.com/gpu
  /var/run/cdi: missing
Generate the specs on the host (...), then refresh the CLI shim ...
```

`missing` means the directory was never mounted into the container. Note
that the runtime no longer stops there: when the local read finds nothing it
reads the same host directories through the Podman socket with a throwaway
`outo-cdi-probe-<random>` container (the Space's own image, the spec
directories bind mounted read-only) and resolves from that. If that works the
device *is* defined, so what remains is Podman's own refusal and nothing on the
host has to change to rule the mount plumbing out; the refresh below is only
about making the diagnosis report the specs instead of the missing mount.

`present` means the directory is there — read the
counts instead: `0 readable specs` with no `(N unreadable)` note is an empty
directory, `(N unreadable)` means the spec files are there but unparseable,
and a `kinds:` list without `nvidia.com/gpu` means the specs were generated
for another vendor. Full recovery for the `missing` case:

```bash
# 1) Generate the specs on the host (nvidia-container-toolkit)
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

# 2) Refresh the shim so it forwards the host spec directories,
#    then recreate the server container so it can read them
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash
outo-models update
```

The installer above already runs step 1 for you: when `nvidia-ctk` is on
`PATH` and `/etc/cdi/nvidia.yaml` does not exist yet, it generates the spec
and prints

```
[ok] generated NVIDIA CDI specs at /etc/cdi/nvidia.yaml
```

It is a best-effort no-op in every other case — hosts without
`nvidia-container-toolkit` stay silent, an existing `/etc/cdi/nvidia.yaml` is
never regenerated or overwritten, and a failed generation warns with the
command above and continues, so the install always finishes. Run the manual
command only when that warning appears (or when the file is gone and you do
not want to re-run the whole installer).

## 13. `outo-models: command not found` after `podman pull`

Pulling the image never installs a host command — the CLI lives inside the
image. Install the host shim once per machine:

```bash
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash
```

If you cannot install the shim (no root), any CLI command still works ad hoc
through podman:

```bash
podman run --rm -it --network=host \
  -v /etc/outo-models:/etc/outo-models \
  -v outo-models-data:/var/lib/outo-models \
  ghcr.io/llaa33219/outo-models:stable --help
```

### "manifest unknown" when the shim pulls
The shim defaults to the `:stable` tag. Before the first stable release is
tagged, that tag does not exist and every shim invocation fails with
`manifest unknown`. Two fixes:

```bash
# Reinstall the shim against the dev track (idempotent):
curl -sSL .../scripts/install-cli.sh | sudo bash -s dev

# …or override per invocation:
OUTO_IMAGE=ghcr.io/llaa33219/outo-models:dev outo-models setup
```

Once the setup wizard has written `image:` into
`/etc/outo-models/config.yaml`, the shim follows that choice automatically.

### The wizard/CLI behaves like an old version (stale `:dev` / `:stable` tag)

Moving tags (`:stable`, `:dev`, `:latest`) are refreshed from the registry on
every shim invocation, so the CLI you run is always the newest published one.
This refresh landed after the first dev builds — if your shim predates it,
re-run the installer. Pinned tags (e.g. `:0.2.0-dev`) are never refreshed;
they are immutable by convention. To bypass the network entirely, set
`OUTO_IMAGE` to a pinned tag or a local build.

### `Error: statfs /etc/outo-models: no such file or directory`

podman does not create missing host bind-mount sources. The installer
creates `/etc/outo-models` (owned by uid 1000 so the rootless container can
write `config.yaml`); if you installed the shim before this fix, create it
once by hand:

```bash
sudo mkdir -p /etc/outo-models && sudo chown 1000:1000 /etc/outo-models
```

Run every `outo-models` command as the SAME user (all rootless as yourself,
or all via sudo) — rootless and rootful podman have separate image stores
and containers, so mixing them makes images/containers seem to "vanish".

### `Error: statfs /run/podman/podman.sock: permission denied` during `update`

`outo-models update` recreates the container by handing `podman run` the HOST
socket path as the bind-mount SOURCE. That path reaches the script only through
`OUTO_PODMAN_SOCK`, which the host CLI shim forwards; shims installed before
v0.6.6 mount the socket at `/run/podman/podman.sock` without forwarding the
host path, so the container-side mount point was passed as the host source and
podman rejected it.

Current versions refuse to guess and print the fix instead. If you still see
the raw error, your shim predates that guard — refresh it and re-run:

```bash
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash
outo-models update
```

The pull and the DB migration already completed when the recreate aborts, and
the running server container is untouched — no downtime.

### `PermissionError` writing `config.yaml` or database files (rootless)

Rootless podman maps the container's uid 1000 to a host subuid by default,
so a bind-mounted host directory owned by you is unwritable from inside the
container. Both the CLI shim and `start` run containers with
`--userns=keep-id` so files stay owned by your host user. If you hit this
before the keep-id fix, the named volume may hold subuid-owned files —
recreate it (safe before the first successful `setup`; destroys all server
data otherwise):

```bash
podman stop outo-models 2>/dev/null; podman rm outo-models 2>/dev/null
podman volume rm outo-models-data
```

## 14. Internal / IP-only mode

The wizard supports an install path where the server is reachable only
from a trusted private network on plain HTTP — no DNS name, no ACME, no
TLS termination. See [install.md](install.md) for the full flow; this
section only covers the operational gotchas.

### `PR_CONNECT_RESET_ERROR` / `ERR_CONNECTION_RESET` in the browser

Internal mode has NO TLS — nothing listens for HTTPS. Browsers that
auto-upgrade bare addresses to `https://` (Firefox HTTPS-First, Chrome
HTTPS-Upgrades) connect to :443, get a reset, and show this error. Type
the scheme explicitly:

```
http://<server-ip>
```

The wizard's final message and the `start` success line both print the
exact `http://…` URL to use.

### HTTP only — no TLS

Caddy binds plain `:80` in internal mode. The Caddyfile has no global
`email` / `acme_ca` / per-site `tls { ... }` blocks. HSTS is suppressed
in the security-headers middleware for the same reason. **Do not expose
port 80 to the public internet in this mode** — transport-layer
encryption is the operator's responsibility at the network edge.

### Cert renewal is skipped

`tls.renewal.renewal_job` short-circuits with `CertHealth(ok=True,
not_after=None, days_remaining=None, error=None)` when `Settings.is_internal`
is True. The scheduler still ticks daily; no network call is made.

### Firewall step is tolerant

When the wizard runs inside a container via the host shim,
`open_ports` raises `firewall_container_host_required`. The wizard
prints the host command (e.g.
`/usr/local/share/outo-models/firewall-open.sh firewalld 80 443`) and
**continues** with the install. The operator must open the ports on the
host by hand before clients can reach the server.

In non-interactive mode the host command is part of the wizard's stdout
output. Run it once with `sudo` (the script self-elevates with `sudo -n`).

### Switching from internal to hostname mode (and back)

The wizard is idempotent. To switch an internal install to hostname
mode, re-run `setup` with a real `--domain` and the rest of the
hostname-mode flags. The Caddyfile is regenerated with TLS enabled and
the wizard runs the DNS step.

To switch back to internal mode, re-run `setup` with `--domain` set to
an IP literal (or omitted). The Caddyfile drops the TLS blocks and the
DNS step is skipped.

## Next steps

- [install.md](install.md) — first-time install steps
- [setup-wizard.md](setup-wizard.md) — exact behavior of each automated
  step
- [security.md](security.md) — authentication / token / audit log policy
