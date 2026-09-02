# Install

`outo-models` ships as a **single Podman image**. The FastAPI app and Caddy
both run inside the container; only the data directory and config file live
on the host. This page walks you from image pull all the way to the first
container run.

> **The CLI also lives inside the image.** Pulling an image never puts an
> `outo-models` command on the host by itself — install the host shim
> (`scripts/install-cli.sh`, section 3) once per machine.

> **The development environment does not have podman** (AGENTS.md §4). Build
> and verify the container on a separate test machine. In this repo's dev
> environment we only guarantee `uv sync` → `make lint` → `make typecheck` →
> `make test`.

## 1. Prerequisites

Check the following on the server host that will run the production
deployment.

- **podman** 4.x or later (check with `podman --version`)
- One of **firewalld / ufw / nftables** (see the "Firewall not detected"
  section of [troubleshooting.md](troubleshooting.md) if none are present)
- Outbound access for ports 80 / 443 (cloud security groups included)
- A delegated DNS name (e.g. `models.example.com`)
- Optional: an email contact for ACME issuance
- Optional: a Cloudflare API token for DNS automation (permission
  `Zone.DNS:Edit`)

## 2. Pulling the image

Two paths — pick exactly one.

### 2-A. Pull the prebuilt image from ghcr.io (recommended)

The release workflow in
[`.github/workflows/release-image.yml`](../.github/workflows/release-image.yml)
publishes these tags automatically, so no build tooling is required.

| Tag | Meaning | When to use |
| --- | --- | --- |
| `:X.Y.Z-stable` | Pinned stable image (e.g. `0.2.0-stable`) | Production, locked to a specific version |
| `:stable` | Rolling stable release | Default for production |
| `:latest` | Latest **stable** release | Default for production (in sync with `:stable`) |
| `:X.Y.Z-dev` | Pinned dev image (includes debugpy / ipython) | Test machines, debugging |
| `:dev` | Most recent dev release | Test machines, debugging |
| `:X.Y.Z-<flavor>-amd64` / `-arm64` | Per-architecture image | Debugging, or pinning a specific arch |

All tags above except the per-arch variants are **manifest lists covering
linux/amd64 and linux/arm64** (built natively on GitHub's amd64 and ARM
runners — no QEMU). `podman pull` on an ARM server (Ampere, AWS Graviton,
Oracle A1, ...) automatically gets the arm64 image.

```bash
# Production server host (pulling the image to run)
sudo podman pull ghcr.io/<owner>/outo-models:stable

# Or pin to a specific version
sudo podman pull ghcr.io/<owner>/outo-models:0.2.0-stable

# Test machine (dev image)
sudo podman pull ghcr.io/<owner>/outo-models:dev
```

`<owner>` is the GitHub user or organization that owns the image. If you
forked this repo as-is, the same `<owner>/outo-models` becomes the registry
path.

### 2-B. Build locally (when you need a custom variant)

If you need to patch the image with your own operational policy or you are
running in an air-gapped environment, build directly from
[Containerfile](../Containerfile). The build fails immediately if
`IMAGE_FLAVOR` is anything other than `stable` or `dev`.

```bash
# Production build (non-root, no debug tooling)
make build-stable

# Development build (includes debugpy / ipython, OUTO_ENV=development)
make build-dev
```

Internally `make` runs:

```bash
podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .
podman build --build-arg IMAGE_FLAVOR=dev    -t outo-models:dev    .
```

The build steps are defined in [Containerfile](../Containerfile). Key points:

- `uv sync --frozen --no-dev --no-editable` to lock dependencies
- `xcaddy build --with github.com/caddy-dns/cloudflare` to build Caddy plus
  the DNS-01 plugin
- The `runtime-base` stage validates `IMAGE_FLAVOR` and creates the
  non-root user (uid/gid 1000)
- The `stable` / `dev` stages branch environment variables and extra
  packages

Do not deploy the `dev` flavor to production. The entrypoint rejects
`IMAGE_FLAVOR=dev` + `OUTO_ENV=production` (AGENTS.md §4).

### Choosing a path

- **Official release + auto-update**: use `:stable` from ghcr.io
- **Official release + version pinning (rollback)**: use `:X.Y.Z-stable`
  from ghcr.io
- **Operational patches or air-gapped**: build locally (e.g.
  `make build-stable`)
- **Testing / debugging**: use `:dev` from ghcr.io or `make build-dev`

### 2-C. Install the host CLI shim (required once per host)

The operator CLI (`outo-models setup`, `start`, `admin`, …) lives inside the
image, so a bare `podman pull` leaves no `outo-models` command on the host.
The shim script writes `/usr/local/bin/outo-models`, a small wrapper that
runs the CLI from the image with the mounts it needs (`/etc/outo-models`,
the `outo-models-data` volume, and the host Podman socket):

```bash
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash
# or, from a cloned repo:
sudo bash scripts/install-cli.sh            # default image tag: stable
sudo bash scripts/install-cli.sh dev        # shim defaults to the dev image
```

After this, `outo-models --help` works on the host. Override the image per
invocation with `OUTO_IMAGE` (e.g. `OUTO_IMAGE=ghcr.io/llaa33219/outo-models:dev outo-models status`).

> Through the shim, the wizard cannot open host firewall ports by itself
> (the firewall tools are not in the image). Run
> `outo-models setup run --skip-firewall`, then open the ports on the host
> once with sudo — see the firewall one-liners in
> [troubleshooting.md](troubleshooting.md).

## 3. Container-external data directory

The default data directory is `/var/lib/outo-models`. Create it on the host
and set permissions ahead of time.

```bash
sudo mkdir -p /var/lib/outo-models
sudo chown -R 1000:1000 /var/lib/outo-models
```

The `setup` wizard will populate this directory with `db.sqlite3`, `repos/`,
`spaces/`, `certs/`, and `audit/`. See
[architecture.md](architecture.md#data-layout) for details.

## 4. First run: the setup wizard

Right after pulling or building the image, run the wizard **on the host
once** to create the config file.

```bash
sudo outo-models setup
```

The first interactive prompt is the **image track** — `stable`
(recommended for production), `dev` (debug tooling), or `custom` (free-form
reference). The choice is written into `config.yaml` and reused by `start`
and `update`, so picking `dev` here means every later `update` will pull
the dev image unless `--image` overrides it. The full prompt order is
documented in [setup-wizard.md](setup-wizard.md).

The command runs the following steps in order:

1. Ask for the image track (`stable` / `dev` / `custom`)
2. Prompt for domain and ACME email
3. Select the DNS provider (`cloudflare` / `manual`)
4. Prompt for the public IPv4 (or auto-detect)
5. Create the admin account
6. Write `config.yaml` (mode `0o600`) including the chosen image
7. Create the DNS A record (or print manual instructions)
8. Open ports 80 / 443 on the host firewall
9. Run DB migrations and store the hashed admin password
10. Render the Caddyfile

The full flow lives in [setup-wizard.md](setup-wizard.md).

For unattended automation, pass the flags instead of using prompts:

```bash
sudo outo-models setup --non-interactive \
  --domain models.example.com \
  --acme-email admin@example.com \
  --dns-provider cloudflare \
  --public-ipv4 203.0.113.10 \
  --admin-username admin \
  --admin-email admin@example.com \
  --admin-password '<a strong password you generated>' \
  --image stable \
  --yes
```

`--image` accepts the same values the interactive prompt does (`stable`,
`dev`, a pinned version like `0.2.0-stable`, or a full reference like
`localhost/outo-models:0.2.0-dev`). Omitting it defaults to the `stable`
track.

Cloudflare mode also needs a token. `OUTO_CLOUDFLARE_API_TOKEN` takes
precedence over `--admin-password`-style flags.

> **OUTO_IMAGE and the host shim.** The `OUTO_IMAGE` env var, which the
> `scripts/install-cli.sh` shim honors, controls which image the shim
> itself runs as — it is the *shim's* image, not the wizard-configured
> image for the production container. The wizard's `--image` choice
> lives in `config.yaml` and is consumed by `start` / `update`. If you
> want both the shim and the managed container to use the same track,
> set `OUTO_IMAGE` once and pass `--image <ref>` to `setup` (or pick
> `custom` interactively).

## 5. Start the container

Once setup completes, start the container with one host-side command:

```bash
sudo outo-models start
```

Internally this runs:

```bash
podman run -d --name outo-models \
  -e OUTO_DATA_DIR=/var/lib/outo-models \
  -e OUTO_SECRET_KEY=... \
  -e OUTO_DOMAIN=models.example.com \
  -e OUTO_REQUIRE_APPROVAL=true \
  -e OUTO_DB_URL=... (optional) \
  -v outo-models-data:/var/lib/outo-models \
  --cap-add NET_BIND_SERVICE \
  -p 80:80 -p 443:443 \
  outo-models:stable
```

`start` reads the `image`, `volume`, and `ports` keys from
`/etc/outo-models/config.yaml` and forwards them. The in-container
entrypoint (`/usr/local/bin/outo-entrypoint.sh`) prints the banner and then
`exec`s `outo-models serve`. See the request flow in
[architecture.md](architecture.md#request-flow).

Confirm the container is up:

```bash
outo-models status
# [status] running: outo-models
```

You can now browse to `https://models.example.com/`. Log in with the admin
account you created during `setup`.

## 6. Post-install checks

Run through these before going live:

- `https://<domain>/admin` renders (admin login required)
- `https://<domain>/api/admin/users` returns 200 with an admin PAT
- `git clone https://<domain>/<admin>/test.git` then a first push succeeds
  (see [git-repos.md](git-repos.md))
- `outo-models status` reports `[status] running`

If anything looks off, head to [troubleshooting.md](troubleshooting.md).

## 7. Upgrading

`outo-models update` pulls the new image, runs migrations, and restarts
the container. By default `update` follows the `image` key in
`/etc/outo-models/config.yaml` (the same value `start` uses); pass
`--image <ref>` to override for a single invocation. See
[cli.md](cli.md#update) and [architecture.md](architecture.md#image-flavors)
for the full flow.

```bash
sudo outo-models update                       # follows config.yaml's image key
sudo outo-models update --image stable        # explicit override
sudo outo-models update --image 0.2.0-stable  # pinned version
```

## Next steps

- [setup-wizard.md](setup-wizard.md) — exactly what the wizard does
- [admin.md](admin.md) — signup approval, quotas, GPU management
- [architecture.md](architecture.md) — data layout and request flow
