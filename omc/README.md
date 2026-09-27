# outo-models-cli

`omc` — the user-facing command-line client for self-hosted
[outo-models](https://github.com/outo-models/outo-models) servers.
Hugging Face CLI surface, adapted to a self-hosted multi-server world.

The CLI is documented entirely in-band: typing `omc` (bare) or
`omc --help` prints a comprehensive reference covering PURPOSE,
CONCEPTS, GETTING STARTED, COMMAND INDEX, CONFIG, and MORE HELP.
Every subcommand (`omc auth login`, `omc repo create`, `omc upload`,
...) has the same six-section layout — PURPOSE / USAGE / EXAMPLES /
EXIT CODES / NOTES — so an AI agent reading one page has the full
mental model of every other page.

## Install

```bash
pip install outo-models-cli
```

Both `omc` and `outo-models-cli` console scripts are installed.

## Quickstart (copy-pasteable)

```bash
# 1. Log in (prompts for a PAT, masked)
omc auth login --server https://models.example.com

# 2. Verify
omc auth whoami
omc auth status

# 3. Create a model repo
omc repo create my-model --kind model --public --description "My first model"

# 4. Browse the tree
omc ls alice/my-model

# 5. Download everything
omc download alice/my-model --local-dir ./my-model

# 6. Upload a folder
omc upload alice/my-model ./checkpoints --path-in-repo weights --message "v1"
```

## Command index

| Command          | Purpose                                              | Auth |
| ---------------- | ---------------------------------------------------- | ---- |
| `omc auth login`    | Verify a PAT and store it for one server.          | no   |
| `omc auth logout`   | Remove the stored credential for one server.       | no   |
| `omc auth whoami`   | Print user, server, role, and token source.        | yes  |
| `omc auth status`   | List every configured server and the default one.  | no   |
| `omc repo create`   | Create a new repository on the target server.      | yes  |
| `omc repo delete`   | Delete a repository (owner or admin only).         | yes  |
| `omc repo list`     | List repositories on the target server.            | yes  |
| `omc ls`            | List one directory of a repository at a revision.  | yes  |
| `omc download`      | Recursively download a repository (resumable, LFS).| yes  |
| `omc upload`        | Upload a file or folder (auto-routes >100 MiB → LFS). | yes  |

For the full PURPOSE / USAGE / EXAMPLES / EXIT CODES / NOTES reference
of any command, run `omc <command> --help` (e.g. `omc upload --help`).

## Environment variables

Every environment variable the CLI reads:

* `OMC_SERVER` — server URL override. Highest priority, but below
  the `--server` flag. Never persisted.
* `OMC_TOKEN` — bearer credential override. Used verbatim, never
  persisted; lets CI jobs run without a config file.
* `OMC_CONFIG_DIR` — full override for the config directory.
* `XDG_CONFIG_HOME` — standard XDG base directory (used when
  `OMC_CONFIG_DIR` is unset).
* `HOME` — fallback (`$HOME/.config/omc`) when neither of the above
  is set.

Resolution order (highest priority first):

```
--server flag  >  OMC_SERVER  >  default_server in config  >
first server in config file
OMC_TOKEN  >  token stored for the resolved server
```

## Configuration

Credentials are stored at `$OMC_CONFIG_DIR/config.json` (defaults to
`$XDG_CONFIG_HOME/omc/config.json` or `~/.config/omc/config.json`).
The file has mode `0600`, owned by the current user; the CLI tightens
the permissions on every save, so a stray `chmod 644` is repaired
automatically.

File shape:

```json
{
  "default_server": "https://models.example.com",
  "servers": {
    "https://models.example.com": {
      "token": "...",
      "username": "alice"
    }
  }
}
```

URL keys are normalized: trailing slashes stripped, scheme + host
lower-cased, `http://` prepended when the input carries no scheme.

## Git LFS

Files larger than 100 MiB are uploaded through Git LFS automatically;
the CLI partitions the input set, runs the LFS batch + PUT dance for
each large file, and commits the resulting pointer text alongside any
small files in a single commit. No `git lfs track` setup is required
on the user side. The download command follows the same path
transparently.

## Exit codes

Every command emits one of:

| Code | Meaning                                                           |
| ---- | ----------------------------------------------------------------- |
| `0`  | Success.                                                          |
| `1`  | Command failed (network, auth, validation, file system).           |
| `2`  | Typer-level usage error (unknown flag, missing required argument).|

Re-run with `--debug` to get a full Python traceback instead of the
single English error line on stderr.

## License

Apache-2.0.
