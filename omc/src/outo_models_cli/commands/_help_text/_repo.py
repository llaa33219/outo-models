"""Help text for `omc repo ...` and every repo subcommand."""

from __future__ import annotations

from outo_models_cli.commands._help_text._common import (
    AUTH_NOTES,
    COMMON_EXIT_CODES,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    NOTES_MARKER,
    PURPOSE_MARKER,
    SERVER_RESOLUTION_ORDER,
    USAGE_MARKER,
)

REPO_SUBCOMMAND_INDEX: str = (
    "REPO SUBCOMMANDS\n"
    "  create    Create a new repository. Defaults: kind=model, visibility=private.\n"
    "  delete    Delete a repository. Prompts for confirmation unless `--yes`.\n"
    "  list      List repositories on the server, optionally filtered by owner / kind."
)

REPO_NOTES_COMMON: str = (
    (
        "NOTES\n"
        "  - Every repo belongs to a kind (`model`, `dataset`, or `space`).\n"
        "    The kind controls the namespace - `alice/foo` (model) and\n"
        "    `alice/foo` (dataset) are distinct repos.\n"
        "  - Visibility defaults to private on `repo create` (matches the\n"
        "    server's default). Passing both `--public` and `--private` is\n"
        "    an error."
    )
    + "\n"
    + AUTH_NOTES
    + "\n"
    + SERVER_RESOLUTION_ORDER
)

# --- create ---------------------------------------------------------------

REPO_CREATE_PURPOSE: str = (
    "PURPOSE\n"
    "  Create a new repository on the resolved server. After creation the\n"
    "  CLI prints the new repo's `owner/name`, visibility, kind, and\n"
    "  git-style clone URL."
)

REPO_CREATE_USAGE: str = (
    "USAGE\n"
    "  omc repo create <name> [--kind model|dataset|space] [--public|--private]\n"
    "                       [--description <text>] [--server <url>]"
)

REPO_CREATE_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc repo create my-model\n"
    "      Creates `my-model` as a private model repo owned by the\n"
    "      authenticated user (default kind + visibility).\n"
    "\n"
    '  omc repo create my-model --public --description "My first model"\n'
    "      Public model repo with a one-line description.\n"
    "\n"
    "  omc repo create sentiment --kind dataset --private\n"
    "      A private dataset repo named `sentiment`.\n"
    "\n"
    '  omc repo create demo --kind space --public --description "Gradio demo"\n'
    "      A public Space repo (requires a `Dockerfile` or `Containerfile`\n"
    "      at the repo root before the Space can be built).\n"
    "\n"
    "  omc repo create my-model --server https://staging.example.com\n"
    "      Create against an alternate server."
)

REPO_CREATE_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Repo created (summary printed to stdout).\n"
        "  1  Invalid kind (not one of model/dataset/space); both `--public`\n"
        "     and `--private` given; missing credential; server rejected the\n"
        "     name (taken, invalid, or forbidden).\n"
        "  2  Typer-level usage error (e.g. missing `<name>`)."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

REPO_CREATE_EPILOG: str = "\n\n".join(
    [REPO_CREATE_EXAMPLES, REPO_CREATE_EXIT_CODES, REPO_NOTES_COMMON],
)

# --- delete ---------------------------------------------------------------

REPO_DELETE_PURPOSE: str = (
    "PURPOSE\n"
    "  Permanently delete a repository on the resolved server. The owner\n"
    "  or any account with the `admin` role may delete. The operation\n"
    "  is irreversible - the git history, all revisions, and every LFS\n"
    "  object are removed."
)

REPO_DELETE_USAGE: str = (
    "USAGE\n"
    "  omc repo delete <owner>/<name> [--kind model|dataset|space]\n"
    "                            [--yes] [--server <url>]"
)

REPO_DELETE_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc repo delete alice/my-model\n"
    "      Interactive confirmation prompt; abort with Ctrl-C to cancel.\n"
    "\n"
    "  omc repo delete alice/my-model --yes\n"
    "      Skip the prompt. Use this in scripts; the operation still\n"
    "      cannot be undone server-side.\n"
    "\n"
    "  omc repo delete alice/old-data --kind dataset --yes\n"
    "      Delete a dataset repo (kind defaults to `model`; pass the right\n"
    "      kind for non-model repos).\n"
    "\n"
    "  omc repo delete alice/my-space --kind space --server https://staging.example.com\n"
    "      Delete a Space repo on an alternate server."
)

REPO_DELETE_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Repo deleted (success line printed).\n"
        "  1  Missing credential; repo not found (404); forbidden (403);\n"
        "     invalid kind; argument missing the `/`; the user aborted the\n"
        "     confirmation prompt.\n"
        "  2  Typer-level usage error."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

REPO_DELETE_EPILOG: str = "\n\n".join(
    [REPO_DELETE_EXAMPLES, REPO_DELETE_EXIT_CODES, REPO_NOTES_COMMON],
)

# --- list -----------------------------------------------------------------

REPO_LIST_PURPOSE: str = (
    "PURPOSE\n"
    "  List every repository on the resolved server, with optional\n"
    "  filters on owner and kind. The output is a Rich table; pipe it\n"
    "  through `column -t -s '|'` or convert with `jq` for machine use."
)

REPO_LIST_USAGE: str = (
    "USAGE\n"
    "  omc repo list [--owner <name>] [--kind model|dataset|space]\n"
    "                [--server <url>]"
)

REPO_LIST_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc repo list\n"
    "      Every repo on the default server, grouped by the server's\n"
    "      default ordering (newest first).\n"
    "\n"
    "  omc repo list --owner alice\n"
    "      Every repo owned by `alice`.\n"
    "\n"
    "  omc repo list --kind dataset\n"
    "      Only dataset repos.\n"
    "\n"
    "  omc repo list --owner alice --kind space --server https://staging.example.com\n"
    "      Combined filters on an alternate server.\n"
    "\n"
    '  omc repo list 2>/dev/null | awk \'NR>2 && $0 != "" {print $2"/"$3}\'\n'
    "      Machine-friendly extraction of `owner/name` pairs (note: header\n"
    "      row detection is heuristic - prefer parsing the API directly\n"
    "      if you need a stable schema)."
)

REPO_LIST_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Table printed (or `No repositories found.` when the filter\n"
        "     matches nothing).\n"
        "  1  Missing credential; invalid kind; server error.\n"
        "  2  Typer-level usage error."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

REPO_LIST_EPILOG: str = "\n\n".join(
    [REPO_LIST_EXAMPLES, REPO_LIST_EXIT_CODES, REPO_NOTES_COMMON],
)

# --- repo app body --------------------------------------------------------

REPO_APP_HELP: str = "\n\n".join(
    [
        "PURPOSE\n"
        "  Create, delete, and list repositories on the resolved server.\n"
        "  All three subcommands require a stored credential.",
        "USAGE\n  omc repo [SUBCOMMAND] [OPTIONS]",
        REPO_SUBCOMMAND_INDEX,
        "Run `omc repo <subcommand> --help` for the full PURPOSE / EXAMPLES /\n"
        "EXIT CODES / NOTES reference of any individual subcommand.",
    ],
)

# --- markers --------------------------------------------------------------

REPO_MARKERS: tuple[str, ...] = (
    PURPOSE_MARKER,
    USAGE_MARKER,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    NOTES_MARKER,
)
REPO_APP_MARKERS: tuple[str, ...] = (PURPOSE_MARKER, USAGE_MARKER)


__all__ = [
    "REPO_APP_HELP",
    "REPO_APP_MARKERS",
    "REPO_CREATE_EPILOG",
    "REPO_DELETE_EPILOG",
    "REPO_LIST_EPILOG",
    "REPO_MARKERS",
]
