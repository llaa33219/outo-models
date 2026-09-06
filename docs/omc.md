# omc — the user-facing CLI

`omc` (PyPI package `outo-models-cli`) is the command-line client for people
who USE an outo-models server (as opposed to `outo-models`, the operator CLI
that RUNS the server). It follows the Hugging Face CLI's shape, adapted for
self-hosted servers.

## Install

```bash
pip install outo-models-cli
# both entry points work:
omc --help
outo-models-cli --help
```

## Targeting a server

Self-hosted means the server could be anywhere, so every command targets a
server explicitly. Resolution order for the target:

1. `--server <url>` flag (per invocation)
2. `OMC_SERVER` environment variable
3. the default server chosen at `omc auth login --set-default`

```bash
omc auth login --server http://<server-ip>
# paste a Personal Access Token (create one at /settings/tokens on the web UI)
```

Credentials are stored per-server in `~/.config/omc/config.json` (mode 0600).
`OMC_TOKEN` overrides the stored token (CI usage). `omc auth status` lists
configured servers; `omc auth logout [--server ...]`; `omc auth whoami`.

## Commands

| Command | What it does |
| --- | --- |
| `omc auth login / logout / whoami / status` | server-targeted credential management |
| `omc repo create <name> --kind model\|dataset\|space [--private]` | create a repository |
| `omc repo delete <owner>/<name> [--kind ...]` | delete (asks unless `--yes`) |
| `omc repo list [--owner ...] [--kind ...]` | list repositories |
| `omc ls <owner>/<name> [--path subdir] [--revision main]` | list files |
| `omc download <owner>/<name> [--revision ...] [--include "*.safetensors"] [--exclude ...] [--local-dir DIR] [--max-workers 8]` | download a repo |
| `omc upload <owner>/<name> <path> [--path-in-repo subdir] [--message ...]` | upload a file or folder |

## Large-data behavior

`download` is built for big repositories: files stream to `<name>.part` and
rename atomically on completion; an interrupted run resumes via HTTP `Range`
when the server's ETag still matches (mismatch → full redownload); transfers
run with bounded parallelism and rich progress bars; nothing is buffered
whole into memory.

`upload` caps single files at 100 MiB (the server's `/upload` limit). Bigger
artifacts belong in git + LFS:

```bash
git lfs track "*.bin"
git add . && git commit -m "add weights" && git push
```

## Notes

- PATs, not passwords: git endpoints and `omc` authenticate with Personal
  Access Tokens only. Create them on the server's web UI under
  `/settings/tokens`.
- `omc` never prints or logs your token.
- `--debug` shows full tracebacks; without it, errors are one-line messages.
