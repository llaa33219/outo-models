# outo-models-cli

`omc` — the user-facing command-line client for self-hosted
[outo-models](https://github.com/outo-models/outo-models) servers.
Hugging Face CLI surface, adapted to a self-hosted multi-server world.

## Install

```bash
pip install outo-models-cli
```

Both `omc` and `outo-models-cli` console scripts are installed.

## Quickstart

```bash
# 1. Log in (prompts for a PAT, masked)
omc auth login --server http://192.168.0.239

# 2. Verify
omc auth whoami

# 3. Create a model repo
omc repo create my-model --kind model --public --description "My first model"

# 4. Browse the tree
omc ls alice/my-model

# 5. Download everything
omc download alice/my-model --local-dir ./my-model

# 6. Upload a folder
omc upload alice/my-model ./checkpoints --path-in-repo weights --message "v1"
```

## Commands

```
omc auth login    --server <url> [--token PAT] [--set-default]
omc auth logout   [--server <url>]
omc auth whoami   [--server <url>]
omc auth status

omc repo create   <name> [--kind model|dataset|space] [--public|--private]
omc repo delete   <owner>/<name> [--kind ...] [--yes]
omc repo list     [--owner ...] [--kind ...]

omc ls            <owner>/<name> [--path subdir] [--revision main]
omc download      <owner>/<name> [--revision main] [--include ...]
                                       [--exclude ...] [--local-dir ./x]
                                       [--max-workers 8]
omc upload        <owner>/<name> <local-path> [--path-in-repo subdir]
                                       [--message ...]
```

Every command accepts `--server <url>` to override the default target.

## Environment variables

* `OMC_SERVER` — overrides the default server URL.
* `OMC_TOKEN`  — supplies the bearer credential directly (highest priority).
* `OMC_CONFIG_DIR` — overrides the config directory (defaults to
  `$XDG_CONFIG_HOME/omc` or `~/.config/omc`).

## Configuration

Credentials are stored at `~/.config/omc/config.json` with mode `0600`.
The file maps each known server URL to its bearer token; the first
login becomes the default target.

## License

Apache-2.0.