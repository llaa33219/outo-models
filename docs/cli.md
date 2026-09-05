# CLI reference

`outo-models` exposes a single Typer console script that handles every
operational task. The source lives in
[src/outo_models/cli/main.py](../src/outo_models/cli/main.py); the entry
point is `outo-models = "outo_models.cli.main:app"` in `pyproject.toml`.

> Every subcommand carries an English help string (`Typer(help=...)`).
> `OutoError` renders as a one-line English message followed by exit code
> 1 — Python tracebacks are never printed (AGENTS.md §2.1).

## Top-level options

```bash
outo-models --version
# outo-models 0.1.0
```

| Option | Meaning |
| --- | --- |
| `--version` | Print the package version and exit immediately |
| `-h`, `--help` | Show help |

Running without arguments prints help (`no_args_is_help=True`).

## Command tree

```
outo-models
├── setup                First-time interactive setup wizard
│   └── run              Wizard body (--non-interactive etc.)
├── server               Commands that run inside the container
│   ├── serve            Boot the FastAPI app under uvicorn
│   └── migrate          alembic upgrade head (DB migration)
├── start                Start the container (host side)
├── stop                 Stop the container
├── restart              Restart the container
├── status               Check the container's running state
├── update               Pull image + DB migration + restart
├── reset                Wipe container and data (3-time confirmation gate)
└── admin                User / quota / GPU management
    ├── list             List users
    ├── pending          List users awaiting approval
    ├── approve <name>   Approve a signup
    ├── deny <name>      Reject a signup
    ├── ban <name>       Ban a user
    ├── unban <name>     Lift a ban
    ├── quota
    │   ├── show <name>  Show storage usage
    │   └── set <name> <size>  Set storage quota
    ├── gpu
    │   ├── show <name>  Show GPU assignment
    │   ├── assign <name> <ids...>  Assign GPUs
    │   └── clear <name>  Clear GPU assignment
    └── reset-password <name>  Generate a new password (printed once)
```

## setup

`setup_app` (Typer sub-app) has exactly one command, `run`. Bare `setup`
(with no flags) is equivalent to `setup run` with defaults; all flags live
on `setup run`. See [setup-wizard.md](setup-wizard.md) for the full prompt
and automation details.

```bash
sudo outo-models setup                                     # interactive
sudo outo-models setup run --non-interactive --yes ...     # non-interactive
```

| Flag | Meaning |
| --- | --- |
| `--non-interactive` | Disable prompts (required flag / env-based) |
| `--domain <domain-or-ip>` | Server hostname (hostname mode) **or** omit / pass an IP literal (internal mode) |
| `--acme-email <email>` | ACME account email — hostname mode only |
| `--dns-provider <cloudflare\|manual>` | DNS provider — hostname mode only |
| `--public-ipv4 <IPv4>` | IPv4 for the DNS A record (hostname mode) **or** LAN IPv4 (internal mode, required in non-interactive) |
| `--admin-username <slug>` | Admin account name |
| `--admin-email <email>` | Admin account email |
| `--admin-password <password>` | Admin password (8+ chars) |
| `--skip-dns` | Skip the DNS step (auto-skipped in internal mode) |
| `--skip-firewall` | Skip the firewall step |
| `--skip-ip-detect` | Skip automatic IPv4 detection (and LAN detection in internal mode) |
| `--yes` | Auto-accept defaults for safe steps |
| `--ports <CSV>` | Comma-separated ports (default `80,443`) |
| `--require-approval` / `--no-require-approval` | Signup approval policy |
| `--image <ref>` | Image track / reference (default: `stable` track → `ghcr.io/llaa33219/outo-models:stable`) |

In **hostname mode** `--domain` must be a real DNS name; `--acme-email`
and `--dns-provider` are required in non-interactive mode.

In **internal / IP mode** omit `--domain` entirely (or pass an IP
literal); the wizard skips the ACME / DNS provider prompts and renders
the Caddyfile in plain HTTP. `--public-ipv4` is required in non-interactive
mode so the wizard knows which address to write into `config.yaml`.

## server

Commands that run **inside** the container. Do not invoke them from the host.

### serve

```bash
outo-models serve [--host 127.0.0.1] [--port 8000]
```

| Flag | Meaning | Default |
| --- | --- | --- |
| `--host <addr>` | uvicorn bind host (Caddy reverse-proxies to it) | `127.0.0.1` |
| `--port <port>` | uvicorn bind port (1–65535) | `8000` |

This is the command invoked by `CMD ["serve"]` in the `Containerfile`. The
`/usr/local/bin/outo-entrypoint.sh` script prints the banner, runs the
dev/prod guard, then `exec`s `outo-models "$@"`.

### migrate

```bash
outo-models migrate
```

Runs `alembic upgrade head` against the configured DB URL. `update.sh`
calls it from a throwaway container. Exits 0 on success, 1 on failure, so
the host script can react to the result.

## start

```bash
sudo outo-models start
```

Reads the `image`, `volume`, and `ports` keys from
`/etc/outo-models/config.yaml` and runs:

```bash
podman run -d --name outo-models \
  -e OUTO_DATA_DIR=... -e OUTO_SECRET_KEY=... -e OUTO_DOMAIN=... \
  -e OUTO_REQUIRE_APPROVAL=true -e OUTO_DB_URL=... \
  -v outo-models-data:/var/lib/outo-models \
  --cap-add NET_BIND_SERVICE \
  -p 80:80 -p 443:443 \
  outo-models:stable
```

If `podman` is missing from `PATH`, an English message is printed to
stderr — "this command must run on the server host" — and the process
exits with code 1.

### Startup verification

`start` does not trust `podman run -d` returning 0. After spawning it
verifies the stack actually came up:

1. polls `podman inspect` for the container state (a container that went
   `exited`/`dead` is detected immediately — no waiting), then
2. polls `http(s)://…/healthz` (loopback in internal mode, the domain
   otherwise; `--verify-timeout` seconds, default 60).

On success it prints `[done] server is up: <url>`. On failure it dumps the
last 50 lines of `podman logs` and exits 1 with `start_verify_failed` — a
container that died on startup (bad config, port bind failure) therefore
never looks "started". Use `--no-verify` to skip verification entirely
(e.g. in CI wrappers that probe on their own).

## stop

```bash
sudo outo-models stop
```

Calls `podman stop outo-models`. If the container does not exist, the
command exits 0 idempotently. The same "podman missing" handling as
`start` applies.

## restart

```bash
sudo outo-models restart
```

Calls `podman restart outo-models`. Behavior and podman-missing handling
match `stop`.

## status

```bash
outo-models status
```

Checks `podman container exists outo-models` and then
`podman inspect ... .State.Running` to determine state. Prints a single
English line:

- `[status] running: outo-models`
- `[status] stopped: outo-models`
- `[status] container not found: outo-models`
- `[info] podman is not installed on this host (development environment).`

`status` is the only command that exits **0** when podman is missing
(informational). Other commands (`start` / `stop` / `restart`) exit 1.

## update

```bash
sudo outo-models update [--image <ref>]
```

Invokes `src/outo_models/assets/scripts/update.sh`, which runs the following steps in
order:

1. `podman pull <image>`
2. `podman run --rm -v outo-models-data:/var/lib/outo-models <image> outo-models migrate`
3. `podman restart outo-models` (only if the container exists)

The script's exit code becomes the CLI's exit code. Anything non-zero is
rendered as `OutoError(code="update_failed")` and the process exits 1.

**Image precedence.** When `--image` is omitted, `update` reads the
`image` key from the same `/etc/outo-models/config.yaml` the `start`
command reads (the value the setup wizard wrote). The `--image` flag,
when passed, is normalized through `normalize_image_ref`: a bare tag
like `stable` or `0.2.0-stable` gets `ghcr.io/llaa33219/outo-models:`
prepended; a full reference containing `/` (e.g.
`localhost/outo-models:stable` for local builds, or a fork's
`ghcr.io/<owner>/outo-models:tag`) passes through unchanged. When both
the flag and the config are missing, `update` falls back to
`ghcr.io/llaa33219/outo-models:stable`.

## reset

```bash
outo-models reset                  # dry-run (default)
outo-models reset --destroy        # actually delete (gate required)
OUTO_DESTRUCTIVE=1 outo-models reset --destroy   # actually delete
```

The **three-`yes` gate (AGENTS.md §2.2) cannot be modified**. The exact
behavior is:

| Invocation | Result |
| --- | --- |
| `outo-models reset` | dry-run summary only, exit 0 |
| `outo-models reset --destroy` | refused without `OUTO_DESTRUCTIVE=1`, exit 1 |
| `OUTO_DESTRUCTIVE=1 outo-models reset` | dry-run (no `--destroy`) |
| `OUTO_DESTRUCTIVE=1 outo-models reset --destroy` | runs after three `yes` confirmations |

Dry-run example output:

```
[dry-run] The following data would be deleted (no actual deletion will happen):
  - users: 12
  - repositories: 47
  - disk usage: 18.42 GiB
  - container: outo-models
  - volume: outo-models-data
  - config files: /etc/outo-models (config.yaml, Caddyfile, …)

To actually delete, pass the --destroy option together with the environment variable OUTO_DESTRUCTIVE=1.
```

A real destroy removes: the `outo-models` container, any other containers
still holding the data volume (leaked throwaway CLI runs — they would
otherwise block volume deletion with "volume is being used"), the
`outo-models-data` volume (all repositories, DB, certificates), the local
data directory (dev installs), and every operator-generated file in the
config directory (`config.yaml`, `Caddyfile`, …) — the machine returns to
the first-install state. The shipped `config.example.yaml` and the config
directory itself are kept (the host shim bind-mounts it).

> Through the host shim, `OUTO_DESTRUCTIVE=1` set in your shell is forwarded
> into the CLI container automatically.
>
> For the destroy path the shim deliberately does NOT mount the data volume
> into the CLI container — a container holding a volume cannot delete it
> (and self-removal mid-sweep is a hard crash). The gate therefore cannot
> count users/repos in that configuration and says "ALL server data"
> instead of showing fabricated zeros. The dry-run (`outo-models reset`)
> keeps the mount and always shows real counts.

The gate is exactly three `yes` prompts read through `input()`.

- The answer must be exactly `yes` (case-sensitive; whitespace, `y`, and
  blank lines are all rejected).
- A single wrong answer aborts immediately and exits 1.
- EOF (Ctrl-D) also aborts safely.

Once all three prompts pass, the following runs:

1. `src/outo_models/assets/scripts/reset.sh` (host-side container / volume cleanup)
2. If a local copy of `data_dir` exists, `shutil.rmtree` on it

On success, stdout shows:

```
[done] outo-models has been reset to a freshly-installed state.
Run `outo-models setup` to configure it again.
```

## admin

`admin_app` commands support both local DB and remote-mode paths.

### Common options

All admin subcommands (except `reset-password`) accept the following two
options:

| Flag | Meaning |
| --- | --- |
| `--api-url <URL>` | Remote server URL (e.g. `https://models.example.com`) |
| `--token <PAT>` | Admin PAT for the remote server |

Passing only one of `--api-url` or `--token` is rejected with
`ConfigError("--api-url and --token must be used together")`. When both
are passed, the command delegates to the server's `/api/admin/*`
endpoints; the output matches the local-mode output.

### list

```bash
outo-models admin list [--status pending|approved|denied|banned]
outo-models admin list --api-url https://models.example.com --token <PAT>
```

Prints the users table to stdout. Columns: `username`, `email`, `role`,
`status`, `id`.

### pending

```bash
outo-models admin pending
```

Shortcut for `admin list --status pending`.

### approve

```bash
outo-models admin approve <username>
```

Moves a `pending` user to `approved`. An `AuditLog(action="user.approve")`
is recorded. If the username does not exist or is already approved/denied,
the command raises `ConflictError` / `NotFoundError`.

### deny

```bash
outo-models admin deny <username> [--reason <text>]
```

Rejects the signup and stores the reason in `Approval.reason`. An
`AuditLog(action="user.deny")` is recorded. The reason must be at most
500 characters.

### ban

```bash
outo-models admin ban <username> [--reason <text>]
```

Moves a `pending` / `approved` / `denied` user to `banned`. Safety rules:
banning yourself is forbidden (`ForbiddenError`); banning another admin is
also `ForbiddenError`. Banning a user who is already banned raises
`ConflictError`.

### unban

```bash
outo-models admin unban <username>
```

Returns a `banned` user to `approved`. The `Approval` row history is
preserved (audit trail).

### quota show

```bash
outo-models admin quota show <username>
```

```
[quota] alice: max=10.00 GiB used=2.34 GiB
```

Prints `max_bytes` / `used_bytes` in human-readable units. If the row is
missing, `repos.quota.ensure_quota_rows` creates it automatically.

### quota set

```bash
outo-models admin quota set <username> <size>
```

`<size>` accepts a human-readable string. `parse_human_bytes` supports all
of:

| Format | Meaning |
| --- | --- |
| `10GiB`, `10gib`, `10GIB` | 2^30 × 10 |
| `500MiB` | 2^20 × 500 |
| `100KB` | 10^3 × 100 |
| `10737418240` | Plain integer (bytes) |

Invalid input raises `ValidationFailedError`. The change records
`AuditLog(action="admin.quota")`.

### gpu show

```bash
outo-models admin gpu show <username>
```

```
[gpu] alice: gpu-0, gpu-1
# or when nothing is assigned:
[gpu] alice: no assignment
```

GPU IDs are stored as a JSON array under
`web_settings(key="gpu:<username>")`.

### gpu assign

```bash
outo-models admin gpu assign <username> gpu-0 gpu-1 gpu-2
```

**Overwrites** the existing assignment. Takes a whitespace-separated list
of IDs. Records `AuditLog(action="admin.gpu")`.

### gpu clear

```bash
outo-models admin gpu clear <username>
```

Removes the assignment entirely. Idempotent — a missing `web_settings`
row is a no-op.

### reset-password

```bash
outo-models admin reset-password <username>
```

**Local-only** command. It does not accept `--api-url` / `--token` —
resetting a password remotely would send plaintext over the network. A
new password is generated via `secrets.token_urlsafe(18)`, stored as an
argon2id hash, and printed to stdout **exactly once**. Operators must
capture it immediately.

```
[reset] new password for alice (will not be shown again):
  AbCdEf_GhIjKlMnOpQrS
```

An `AuditLog(action="admin.reset_password")` is recorded alongside the
change.

## Environment variables

Pydantic Settings maps every `OUTO_*` environment variable by stripping the
`OUTO_` prefix.

| Variable | Settings field | Default | Meaning |
| --- | --- | --- | --- |
| `OUTO_DATA_DIR` | `data_dir` | `/var/lib/outo-models` | Root for the DB, git repos, LFS, and cert cache |
| `OUTO_DOMAIN` | `domain` | `localhost` | Server address: a hostname (https) or an IP literal / empty (internal mode, plain http) |
| `OUTO_DB_URL` | `db_url` | `null` (→ `sqlite+aiosqlite:///${OUTO_DATA_DIR}/db.sqlite3`) | SQLAlchemy URL |
| `OUTO_SECRET_KEY` | `secret_key` | `""` | Session / token signing key (32+ chars in production) |
| `OUTO_ENV` | `env` | `development` | `development` or `production` |
| `OUTO_REQUIRE_APPROVAL` | `require_approval` | `true` | Require admin approval on signup |
| `OUTO_DEFAULT_QUOTA_BYTES` | `default_quota_bytes` | `10737418240` (10 GiB) | Default quota for new users |
| `OUTO_LFS_BACKEND` | `lfs_backend` | `local` | LFS backend (`local` / `s3`) |
| `OUTO_LFS_MAX_OBJECT_BYTES` | `lfs_max_object_bytes` | `5368709120` (5 GiB) | Max size of a single LFS object |
| `OUTO_S3_ENDPOINT` | `s3_endpoint` | `""` | S3-compatible endpoint URL |
| `OUTO_S3_BUCKET` | `s3_bucket` | `""` | S3 bucket name |
| `OUTO_S3_REGION` | `s3_region` | `us-east-1` | S3 region |
| `OUTO_S3_ACCESS_KEY` | `s3_access_key` | `""` | S3 access key id |
| `OUTO_S3_SECRET_KEY` | `s3_secret_key` | `""` | S3 secret access key |
| `OUTO_S3_PREFIX` | `s3_prefix` | `lfs` | Object-key prefix inside the bucket |
| `OUTO_S3_PRESIGN_TTL_SECONDS` | `s3_presign_ttl_seconds` | `3600` | Lifetime of presigned URLs |
| `OUTO_SPACES_RUNTIME_ENABLED` | `spaces_runtime_enabled` | `false` | Toggle the Spaces container runtime |
| `OUTO_PODMAN_SOCKET` | `podman_socket` | `/run/podman/podman.sock` | Podman REST API Unix socket |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` | `spaces_runtime_port_range_start` | `20000` | Start of Space host-port range |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_END` | `spaces_runtime_port_range_end` | `21000` | End of Space host-port range |

Additional operator-facing variables:

| Variable | Meaning | Where it's read |
| --- | --- | --- |
| `OUTO_CONFIG` | YAML config path | `setup`, `start` (default `/etc/outo-models/config.yaml`) |
| `OUTO_DESTRUCTIVE` | Safety gate for `reset --destroy` | Only `1` passes the gate |
| `OUTO_CLOUDFLARE_API_TOKEN` | Create DNS records in Cloudflare mode | setup wizard (equivalent to `--admin-password`) |
| `OUTO_FIREWALL_SCRIPT` | Override the firewall host-script path | `firewall.open_ports` (default `src/outo_models/assets/scripts/firewall-open.sh`) |
| `OUTO_CADDYFILE_TEMPLATE` | Override the Caddyfile template path | `tls.caddy_manager` (default `src/outo_models/assets/caddy/Caddyfile.j2`) |
| `OUTO_UPDATE_SCRIPT` | Override `update.sh` path | `cli.container_script` |
| `OUTO_RESET_SCRIPT` | Override `reset.sh` path | `cli.container_script` |
| `OUTO_CADDY_ADMIN_URL` | Caddy admin API base URL | lifespan cert health check (default `http://localhost:2019`) |
| `CLOUDFLARE_API_TOKEN` | Used for Caddy DNS-01 | Caddyfile `tls { dns cloudflare {env.CLOUDFLARE_API_TOKEN} }` |

## Exit codes

- `0` — success
- `1` — `OutoError` (single-line English message) or host script failure
- Other codes — explicitly returned by a host script (e.g. alembic failure
  inside `update.sh`)

## Next steps

- [admin.md](admin.md) — operational scenarios for admin commands
- [setup-wizard.md](setup-wizard.md) — full setup prompt details
- [security.md](security.md) — safety gates / token policy
