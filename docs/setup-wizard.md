# Setup wizard (`outo-models setup`)

`outo-models setup` is the interactive / non-interactive configuration tool
for a new install. It runs once on the host to create
`/etc/outo-models/config.yaml` and handle DNS, firewall, database, and
admin account creation in one pass. The wizard is **idempotent** — you can
re-run it later with the same arguments to rotate a password or correct a
setting.

This page is a direct mirror of what
[cli/setup/_collect.py](../src/outo_models/cli/setup/_collect.py) and
[cli/setup/_effect.py](../src/outo_models/cli/setup/_effect.py) actually do.
If you spot a divergence, open a PR to update the docs.

## One-liner

```bash
sudo outo-models setup
```

In interactive mode, the prompts appear in the order below. Each default is
the value picked automatically when `--yes` is passed.

| # | Prompt | Validation / notes |
| --- | --- | --- |
| 1 | `Which image track?` (followed by `[1] stable`, `[2] dev`, `[3] custom`) | `stable` is the recommended default; `dev` includes debug tooling; `custom` triggers a free-form text prompt (`Image reference or tag:`) validated + normalized through `normalize_image_ref`. |
| 2 | `Enter the server domain (e.g. models.example.com):` | `validate_domain` — rejects whitespace / slashes, lowercases |
| 3 | `Enter the ACME (Let's Encrypt) account email:` | The address that receives expiry warnings |
| 4 | `Choose the DNS provider (cloudflare / manual):` | Only `cloudflare` or `manual` accepted |
| 5 | `Enter the Cloudflare API token (Zone.DNS:Edit permission):` | Only when the DNS provider is `cloudflare` (hidden input) |
| 6 | `Server's public IPv4 address (DNS A record):` | If `--skip-ip-detect` is not set, `https://api.ipify.org` is used for auto-detection and shown as the default |
| 7 | `Admin account name (slug, e.g. admin):` | `validate_slug` (lowercase letters / digits / `.` `_` `-`, 1–63 chars) |
| 8 | `Enter the admin account email:` | Must contain `@` |
| 9 | `Enter the admin password (8+ characters):` | Minimum 8 characters |
| 10 | `Re-enter the admin password:` | Must match the first entry |
| 11 | `External ports to open (comma-separated, default 80,443):` | Each port must be in 1–65535 |
| 12 | `Require admin approval for new signups?` | Default `true` (y/N) |

The image track is the **first** prompt because the choice frames every
later step — the in-container process is the same image, and the
`start` / `update` commands read the `image` key the wizard writes into
`config.yaml`.

In `--non-interactive` mode, every value above must come from a flag or an
environment variable. Any missing value exits immediately with `ConfigError`.

## Flags

| Flag | Meaning | Default |
| --- | --- | --- |
| `--non-interactive` | Disable interactive prompts; rely on flags / env vars | `false` |
| `--domain <domain>` | Server domain | (none) |
| `--acme-email <email>` | ACME (Let's Encrypt) account email | (none) |
| `--dns-provider <name>` | `cloudflare` or `manual` | (none) |
| `--public-ipv4 <IPv4>` | Public IPv4 for the DNS A record | (none) |
| `--admin-username <slug>` | Admin account name | (none) |
| `--admin-email <email>` | Admin account email | (none) |
| `--admin-password <password>` | Admin password (8+ characters) | (none) |
| `--skip-dns` | Skip the DNS record creation step | `false` |
| `--skip-firewall` | Skip the firewall port-opening step | `false` |
| `--skip-ip-detect` | Skip automatic IPv4 detection | `false` |
| `--yes` | Auto-accept defaults for safe steps | `false` |
| `--ports <CSV>` | Comma-separated port list | `80,443` |
| `--require-approval` / `--no-require-approval` | Signup approval policy | `true` |
| `--image <ref>` | Image reference — track/version tag (`stable`, `0.2.0-stable`) or full ref (`localhost/outo-models:stable`, `ghcr.io/<fork>/outo-models:tag`) | stable track |

## What the automated steps actually do

`_run_setup` executes the steps below in order. Each step is not in a single
transaction — they can complete partially (which makes idempotent re-runs
safe).

### 1) Environment variable injection (`apply_settings_env`)

The collected values are pushed in as `OUTO_DOMAIN`, `OUTO_REQUIRE_APPROVAL`,
and `OUTO_ENV`. If `OUTO_SECRET_KEY` is unset, a new key is generated via
`secrets.token_urlsafe(48)`. The `Settings` LRU cache is cleared with
`cache_clear()` so subsequent reads pick up the new values.

### 2) Writing `config.yaml` (`write_config`)

If `OUTO_CONFIG` is set, the file is written to that path; otherwise it
goes to `/etc/outo-models/config.yaml`. The keys below are emitted via
`yaml.safe_dump`. **The file mode is `0o600`**; on failure a warning is
written to stderr.

- `version` — package version
- `domain`, `acme_email`, `public_ipv4`, `dns_provider`
- `image` — the full reference the operator chose (the first interactive
  prompt, or `--image <ref>` in non-interactive mode). `start` and
  `update` both read this key.
- `volume` — defaults to `outo-models-data`
- `ports` — the list provided by the operator
- `require_approval`
- `admin_username`, `admin_email`
- `cloudflare_api_token` (cloudflare mode only)
- `secret_key` (only when present in the environment)

The file contains secret keys and DNS API tokens in plaintext, so the
wizard writes a warning to stderr reminding the operator to keep the
`0o600` permission.

### 3) DNS A record (`ensure_dns_record`)

Unless `--skip-dns` is passed:

- `outo_models.dns.factory.create_provider` builds the `cloudflare` or
  `manual` implementation
- A `DnsRecord(name=<domain>, type="A", value=<IPv4>, ttl=300)` is ensured
- In `manual` mode, `ManualProvider.instructions()` prints the operator
  instructions to stdout and the wizard waits for the operator to press
  Enter (`prompts.confirm(default=True)`)

See [dns-providers.md](dns-providers.md) for the detailed behavior.

### 4) Firewall (`open_firewall_ports`)

Unless `--skip-firewall` is passed:

- `outo_models.firewall.detect.detect_firewall()` identifies the backend
  (firewalld → ufw → nftables → none)
- `outo_models.firewall.open_ports.open_ports(ports=...)` invokes the host
  script
- The host script is executed as `bash firewall-open.sh <kind> <port...>`
- When not running as root, `sudo -n` is attached automatically
  (`firewall-open.sh` runs with `set -euo pipefail`)

If `sudo -n` fails, the wizard raises `OutoError(code="firewall_permission")`
and converts it into a `ConfigError` telling the operator to re-run as root
or to add a NOPASSWD rule for `/etc/sudoers.d/outo-models`.

See the firewall section in [troubleshooting.md](troubleshooting.md).

### 5) Data directory + DB + admin account (`bootstrap_database`)

`utils.paths.ensure_dirs()` creates five directories (`repos`, `spaces`,
`certs`, `audit`, and the root), then:

1. `outo_models.db.run_migrations(engine)` — `alembic upgrade head`
2. `outo_models.auth.passwords.hash_password(answers.admin_password)` —
   argon2id hash
3. Inside `session_scope()`, look up the `User`:
   - If found: refresh the email / hash / `role=admin` /
     `status=approved`
   - If not: insert a new `User(role="admin", status="approved",
     approved_at=now)`
4. Call `dispose_engines()`

No password is ever echoed again after the wizard. If it is lost, run
`outo-models admin reset-password <username>` to set a new one.

### 6) Caddyfile rendering (`render_caddyfile_setup`)

`outo_models.tls.caddy_manager.render_caddyfile` renders
[container/caddy/Caddyfile.j2](../container/caddy/Caddyfile.j2) to
`/etc/outo-models/Caddyfile`. `TlsConfig.dns_provider` is only active when
the provider is `cloudflare`.

See the "Caddy and ACME" section of [security.md](security.md#caddy-and-acme)
for the rendered output.

### 7) Next-step banner

The wizard prints the following to stdout:

```
[done] Configuration saved.
  - Config file: /etc/outo-models/config.yaml
  - Caddyfile: /etc/outo-models/Caddyfile
  - Image: ghcr.io/llaa33219/outo-models:stable

Start the server with:
  outo-models start

Passwords are never echoed again. To recover, use admin reset-password.
```

The `Image:` line echoes the exact reference that was written to
`config.yaml` — copy it when pinning a custom registry or when
troubleshooting `update` failures.

## Non-interactive examples

```bash
# Cloudflare automatic mode
sudo OUTO_CLOUDFLARE_API_TOKEN=<token> \
  outo-models setup --non-interactive \
    --domain models.example.com \
    --acme-email admin@example.com \
    --dns-provider cloudflare \
    --public-ipv4 203.0.113.10 \
    --admin-username admin \
    --admin-email admin@example.com \
    --admin-password "$(openssl rand -base64 24)" \
    --yes
```

```bash
# Manual DNS mode (firewall only; DNS done by the operator on the host)
sudo outo-models setup --non-interactive \
    --domain models.example.com \
    --acme-email admin@example.com \
    --dns-provider manual \
    --public-ipv4 203.0.113.10 \
    --admin-username admin \
    --admin-email admin@example.com \
    --admin-password "$(openssl rand -base64 24)" \
    --skip-dns --yes
```

## Next steps

- [install.md](install.md) — start the container for the first time
- [admin.md](admin.md) — signup approval, quotas, GPU management
- [troubleshooting.md](troubleshooting.md) — firewall permissions / port
  binding errors
