# outo-models

A fully open-source, self-hostable model hub server. Modeled on Hugging Face
and ModelScope, it lets you share **models, datasets, and Spaces** over plain
git. After installation, port opening, HTTPS certificate issuance/renewal, DNS
records, and updates are all handled automatically.

## Features

- **Fully automated install**: a single `outo-models setup` run opens the
  firewall, issues and renews ACME (Let's Encrypt) HTTPS certificates, and
  configures DNS records (Cloudflare plugin plus a manual mode). Internal
  networks work too: skip the domain and the server runs on plain HTTP over
  an IP address.
- **git-native repositories**: clone and push model, dataset, and Space repos
  with `git clone` / `git push` directly. Git LFS supported (local or S3
  object storage).
- **omc — the user CLI**: HF-style client for downloading/uploading
  artifacts and managing repos from any machine (see below).
- **Membership management**: signup/login, admin-gated approval (toggleable),
  user bans, storage quotas, and GPU assignments — all from the CLI.
- **Security-first**: argon2 password hashes, PASETO v4 API tokens, security
  headers, rate limits, and an audit log.
- **Multi-arch single-image deployment** (linux/amd64 + linux/arm64) with two
  flavors: `stable` and `dev`.

## Run a server

Everything runs through one container image. The operator CLI lives in the
image — a one-time shim install puts an `outo-models` command on the host
(it also enables the rootless podman socket and installs the host-side
firewall/sysctl helper scripts):

```bash
# 1. Install the host CLI shim (writes /usr/local/bin/outo-models)
curl -sSL https://raw.githubusercontent.com/llaa33219/outo-models/main/scripts/install-cli.sh | sudo bash

# 2. Pull the server image (amd64 and arm64 are both served automatically)
sudo podman pull ghcr.io/llaa33219/outo-models:stable

# 3. Initial setup — the wizard asks the image track, domain (blank =
#    internal/IP mode), DNS provider, admin account, and ports
outo-models setup

# 4. Run the server (verifies the server actually answers; prints the URL)
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

The shim resolves which image to run in this order: `OUTO_IMAGE` env →
the `image:` key in `/etc/outo-models/config.yaml` (chosen by `setup`) →
the install-time default. So once `setup` picks a track, every later
`outo-models` invocation follows it automatically. To update the shim
itself, re-run `install-cli.sh` (idempotent); CLI code updates arrive with
the image via `outo-models update`.

> **Note:** pulling the image alone does not create a host command — step 1
> is what puts `outo-models` on your PATH. You can also run any CLI command
> ad hoc: `podman run --rm ghcr.io/llaa33219/outo-models:stable --help`.

## Use a server: omc

`omc` (PyPI package `outo-models-cli`) is the HF-style client for anyone
using an outo-models server. Since servers are self-hosted anywhere, every
command targets a server explicitly:

```bash
# install — either works
uv tool install outo-models-cli
pip install outo-models-cli

# point at a server + authenticate (create a token at /settings/tokens first)
omc auth login --server https://models.example.com

omc repo list
omc repo create my-model --kind model --public
omc upload alice/my-model ./weights.safetensors
omc download alice/my-model --local-dir ./out
```

Large transfers stream with progress bars, resume interrupted downloads via
HTTP Range, and rename atomically from `.part` files. Full reference:
[docs/omc.md](docs/omc.md).

## Documentation

See [docs/index.md](docs/index.md) for the full documentation set —
[install guide](docs/install.md), [operator CLI reference](docs/cli.md),
[omc (user CLI)](docs/omc.md), [architecture](docs/architecture.md),
[security](docs/security.md), and more.

## License

Apache-2.0 — see [LICENSE](LICENSE).
