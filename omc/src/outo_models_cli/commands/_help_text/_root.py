"""Root `omc` help — shown by `omc` (bare) and `omc --help`.

The root help is the one an AI agent reads first; every other command's
help links back to it for context. Sections follow the layout contract
documented in the parent `_help_text` package.
"""

from __future__ import annotations

ROOT_HELP_PURPOSE: str = (
    "PURPOSE\n"
    "  Command-line client for self-hosted outo-models servers. Drives the\n"
    "  same surface as `huggingface-cli` (auth, repo CRUD, ls, download,\n"
    "  upload) against any outo-models-compatible server."
)

ROOT_HELP_USAGE: str = (
    "USAGE\n"
    "  omc [GLOBAL OPTIONS] COMMAND [ARGS]...\n"
    "  omc COMMAND --help   # per-command reference (PURPOSE, EXAMPLES,\n"
    "                       # EXIT CODES, NOTES)"
)

ROOT_HELP_CONCEPTS: str = (
    "CONCEPTS\n"
    "  Repository   A versioned file tree (model / dataset / space) backed\n"
    "               by a git repo on the server. Every repo is uniquely named\n"
    "               `<owner>/<name>` within its kind; the kind is one of\n"
    "               `model`, `dataset`, `space`.\n"
    "  Auth token  A Personal Access Token (PASETO v4) the server issues to\n"
    "               a user. The CLI stores it locally and uses it as a\n"
    "               `Bearer <token>` header. Tokens are never printed, never\n"
    "               logged, and never echoed in --help output.\n"
    "  LFS         Git Large File Storage. Files larger than 100 MiB are\n"
    "               uploaded through the LFS batch + PUT protocol and\n"
    "               committed to the repo as pointer text; downloads follow\n"
    "               the same path transparently. No `git lfs track` setup\n"
    "               is required on the user side.\n"
    "  Revision    A branch name, tag, or full commit SHA. Defaults to `main`\n"
    "               for every read-side command (`ls`, `download`)."
)

ROOT_HELP_GETTING_STARTED: str = (
    "GETTING STARTED (copy-pasteable)\n"
    "  # 1. Log in to your self-hosted server (prompts for a PAT, masked)\n"
    "  omc auth login --server https://models.example.com\n"
    "\n"
    "  # 2. Confirm the credential works\n"
    "  omc auth whoami\n"
    "  omc auth status\n"
    "\n"
    "  # 3. Create a model repo (default kind = `model`, default visibility = private)\n"
    '  omc repo create my-model --description "My first model"\n'
    "\n"
    "  # 4. Browse the tree\n"
    "  omc ls <your-username>/my-model\n"
    "\n"
    "  # 5. Pull everything to a local directory\n"
    "  omc download <your-username>/my-model --local-dir ./my-model\n"
    "\n"
    "  # 6. Push a folder (or a single file)\n"
    '  omc upload <your-username>/my-model ./checkpoints --path-in-repo weights --message "v1"\n'
    "\n"
    "  # 7. Tear down a credential when you are done with that server\n"
    "  omc auth logout --server https://models.example.com"
)

ROOT_HELP_COMMAND_INDEX: str = (
    "COMMAND INDEX\n"
    "  auth login     Verify a PAT and store it for one server.            [no auth]\n"
    "  auth logout    Remove the stored credential for one server.         [no auth]\n"
    "  auth whoami    Print user, server, role, and token source.          [auth]\n"
    "  auth status    List every configured server and the default one.    [no auth]\n"
    "\n"
    "  repo create    Create a new repository on the target server.        [auth]\n"
    "  repo delete    Delete a repository (owner or admin only).           [auth]\n"
    "  repo list      List repositories on the target server.              [auth]\n"
    "\n"
    "  ls             List one directory of a repository at a revision.    [auth]\n"
    "  download       Recursively download a repository (resumable, LFS).  [auth]\n"
    "  upload         Upload a file or folder (auto-routes >100 MiB → LFS).[auth]\n"
    "\n"
    "  Lines marked `[auth]` require a credential for the resolved server;\n"
    "  `[no auth]` reads/writes the local config file only."
)

ROOT_HELP_CONFIG: str = (
    "CONFIG\n"
    "  Config file   $OMC_CONFIG_DIR/config.json\n"
    "                (defaults to $XDG_CONFIG_HOME/omc/config.json or\n"
    "                ~/.config/omc/config.json). Mode 0600, owned by the\n"
    "                current user. The CLI tightens the permissions on every\n"
    "                save, so a stray `chmod 644` is repaired automatically.\n"
    "\n"
    "  File shape\n"
    "    {\n"
    '      "default_server": "https://models.example.com",\n'
    '      "servers": {\n'
    '        "https://models.example.com": {\n'
    '          "token": "...",\n'
    '          "username": "alice"\n'
    "        }\n"
    "      }\n"
    "    }\n"
    "\n"
    "  URL keys are normalized: trailing slashes stripped, scheme + host\n"
    "  lower-cased, `http://` prepended when the input carries no scheme.\n"
    "\n"
    "  ENVIRONMENT VARIABLES (every var the CLI reads)\n"
    "    OMC_SERVER       Server URL override. Highest priority, but below\n"
    "                     the `--server` flag. Never persisted.\n"
    "    OMC_TOKEN        Bearer credential override. Used verbatim, never\n"
    "                     persisted; lets CI jobs run without a config file.\n"
    "    OMC_CONFIG_DIR   Full override for the config directory. The path\n"
    "                     is created (mode 0700) on first write.\n"
    "    XDG_CONFIG_HOME  Standard XDG base directory. `$XDG_CONFIG_HOME/omc`\n"
    "                     is used when `OMC_CONFIG_DIR` is unset.\n"
    "    HOME             Fallback when neither `OMC_CONFIG_DIR` nor\n"
    "                     `XDG_CONFIG_HOME` is set (`$HOME/.config/omc`).\n"
    "\n"
    "  PRECEDENCE\n"
    "    --server flag  >  OMC_SERVER  >  default_server in config  >\n"
    "    first server in config file\n"
    "    OMC_TOKEN  >  token stored for the resolved server"
)

ROOT_HELP_MORE: str = (
    "MORE HELP\n"
    "  omc auth --help           # auth subcommand index\n"
    "  omc repo --help           # repo subcommand index\n"
    "  omc ls --help             # full ls reference (PURPOSE, EXAMPLES, ...)\n"
    "  omc download --help       # full download reference\n"
    "  omc upload --help         # full upload reference (LFS behaviour)\n"
    "  omc auth login --help     # full login reference\n"
    "  omc <command> <sub> --help\n"
    "\n"
    "GLOBAL OPTIONS\n"
    "  --version       Print the package version and exit (exit 0).\n"
    "  --debug         On error, print the full Python traceback instead of\n"
    "                  the single English line. Off by default.\n"
    "  --help, -h      Show this message and exit."
)

ROOT_HELP: str = "\n\n".join(
    [
        ROOT_HELP_PURPOSE,
        ROOT_HELP_USAGE,
        ROOT_HELP_CONCEPTS,
        ROOT_HELP_GETTING_STARTED,
        ROOT_HELP_COMMAND_INDEX,
        ROOT_HELP_CONFIG,
        ROOT_HELP_MORE,
    ],
)

# Root-level markers — asserted on the bare `omc` / `omc --help` output.
from outo_models_cli.commands._help_text._common import (  # noqa: E402
    COMMAND_INDEX_MARKER,
    CONCEPTS_MARKER,
    CONFIG_MARKER,
    ENVIRONMENT_VARIABLES_MARKER,
    GETTING_STARTED_MARKER,
    MORE_HELP_MARKER,
    PURPOSE_MARKER,
    USAGE_MARKER,
)

ROOT_MARKERS: tuple[str, ...] = (
    PURPOSE_MARKER,
    USAGE_MARKER,
    CONCEPTS_MARKER,
    GETTING_STARTED_MARKER,
    COMMAND_INDEX_MARKER,
    CONFIG_MARKER,
    ENVIRONMENT_VARIABLES_MARKER,
    MORE_HELP_MARKER,
)


__all__ = ["ROOT_HELP", "ROOT_MARKERS"]
