# outo-models

A fully open-source, self-hostable model hub server. Modeled on Hugging Face
and ModelScope, it lets you share **models, datasets, and Spaces** over plain
git. After installation, port opening, HTTPS certificate issuance/renewal, DNS
records, and updates are all handled automatically.

## Features

- **Fully automated install**: a single `outo-models setup` run opens the
  firewall, issues and renews ACME (Let's Encrypt) HTTPS certificates, and
  configures DNS records (Cloudflare plugin plus a manual mode).
- **git-native repositories**: clone and push model, dataset, and Space repos
  with `git clone` / `git push` directly. Git LFS supported (local or S3
  object storage).
- **Membership management**: signup/login, admin-gated approval (toggleable),
  user bans, storage quotas, and GPU assignments — all from the CLI.
- **Security-first**: argon2 password hashes, PASETO v4 API tokens, security
  headers, rate limits, and an audit log.
- **Multi-arch single-image deployment** (linux/amd64 + linux/arm64) with two
  flavors: `stable` and `dev`.

## Quick start

Everything runs through one container image. The CLI itself also lives in the
image — a one-time shim install puts an `outo-models` command on the host.

```bash
# 1. Install the host CLI shim (writes /usr/local/bin/outo-models)
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash

# 2. Pull the server image (amd64 and arm64 are both served automatically)
sudo podman pull ghcr.io/llaa33219/outo-models:stable

# 3. Initial setup (interactive wizard: domain, DNS, admin account, ports)
outo-models setup

# 4. Run the server
outo-models start
```

Operate:

```bash
outo-models status     # container status
outo-models restart
outo-models update     # pull latest image + migrate + restart
outo-models reset      # full wipe (requires three literal 'yes' confirmations)
```

To run a dev-flavor image through the shim:

```bash
OUTO_IMAGE=ghcr.io/llaa33219/outo-models:dev outo-models status
```

> **Note:** pulling the image alone does not create a host command — step 1
> is what puts `outo-models` on your PATH. You can also run any CLI command
> ad hoc: `podman run --rm ghcr.io/llaa33219/outo-models:stable --help`.

See [docs/index.md](docs/index.md) for the full documentation set —
[install guide](docs/install.md), [CLI reference](docs/cli.md),
[architecture](docs/architecture.md), [security](docs/security.md), and more.

## Development

```bash
uv sync
make lint typecheck test
```

Read [AGENTS.md](AGENTS.md) before contributing — it is the binding contract
for everyone working in this repository.

## License

Apache-2.0 — see [LICENSE](LICENSE).
