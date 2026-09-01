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
  with `git clone` / `git push` directly.
- **Membership management**: signup/login, admin-gated approval (toggleable),
  user bans, storage quotas, and GPU assignments — all from the CLI.
- **Security-first**: argon2 password hashes, PASETO v4 API tokens, security
  headers, rate limits, and an audit log.
- **Single-image Podman deployment**: two image flavors — `stable` and `dev`.

## Quick start

```bash
# Build the image (on the test machine)
podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .

# Initial setup (interactive wizard: domain, DNS, admin account, ports)
outo-models setup

# Operate
outo-models start
outo-models restart
outo-models status
outo-models update

# Full reset (requires three 'yes' confirmations)
outo-models reset
```

See [docs/index.md](docs/index.md) for the full documentation set.

## Development

```bash
uv sync
make lint typecheck test
```

Read [AGENTS.md](AGENTS.md) before contributing — it is the binding contract
for everyone working in this repository.
