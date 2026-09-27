"""Structured help text for every `omc` command and the root app.

Every block follows the same six-section layout so an AI reading the
help output can locate the bit it needs without scanning for prose:

    PURPOSE      one paragraph: what the command does, when to use it
    USAGE        exact invocation shape (positional args + flag list)
    ARGUMENTS    covered by Typer's auto-generated table; here we list
                 the env-var overrides that affect argument resolution
    EXAMPLES     3-6 realistic invocations, each with a one-line note
    EXIT CODES   every exit value the command can produce and its meaning
    NOTES        auth requirements, config file location, server URL
                 resolution order, common error strings + fixes

The constants are plain strings (no `rich.console.Console` rendering),
so a non-TTY pipeline (`omc --help | grep EXAMPLES`) reads them cleanly.
`rich_markup_mode="rich"` is set on the root app, so the `[bold]` /
`[dim]` tags inside the constants are processed by Typer when the help
is rendered through a TTY and rendered verbatim otherwise.

Why a dedicated subpackage: the six-section layout must be CONSISTENT
across every command, and the markers are referenced both from the
command definitions (`epilog=...`) and from the test suite (asserted on
`omc <cmd> --help` output). Concentrating them in one import path
keeps the markers and the prose in lock-step so a one-place edit
cannot drift out of sync with the tests, while splitting the bodies
across per-command files keeps each module under the 250-LOC ceiling.
"""

from __future__ import annotations

from outo_models_cli.commands._help_text._auth import (
    AUTH_APP_HELP,
    AUTH_APP_MARKERS,
)
from outo_models_cli.commands._help_text._auth_cmds import (
    AUTH_LOGIN_EPILOG,
    AUTH_LOGIN_MARKERS,
    AUTH_LOGOUT_EPILOG,
    AUTH_LOGOUT_MARKERS,
    AUTH_STATUS_EPILOG,
    AUTH_STATUS_MARKERS,
    AUTH_WHOAMI_EPILOG,
    AUTH_WHOAMI_MARKERS,
)
from outo_models_cli.commands._help_text._common import (
    COMMAND_INDEX_MARKER,
    COMMON_EXIT_CODES,
    CONCEPTS_MARKER,
    CONFIG_MARKER,
    CREDENTIAL_RESOLUTION_ORDER,
    ENVIRONMENT_VARIABLES_MARKER,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    GETTING_STARTED_MARKER,
    MORE_HELP_MARKER,
    NOTES_MARKER,
    PURPOSE_MARKER,
    SERVER_RESOLUTION_ORDER,
    USAGE_MARKER,
)
from outo_models_cli.commands._help_text._repo import (
    REPO_APP_HELP,
    REPO_APP_MARKERS,
    REPO_CREATE_EPILOG,
    REPO_DELETE_EPILOG,
    REPO_LIST_EPILOG,
    REPO_MARKERS,
)
from outo_models_cli.commands._help_text._root import ROOT_HELP, ROOT_MARKERS
from outo_models_cli.commands._help_text._transfer import (
    DOWNLOAD_EPILOG,
    LS_EPILOG,
    TRANSFER_MARKERS,
    UPLOAD_EPILOG,
)

# Required markers per command - the test suite asserts on each entry.
COMMAND_MARKERS: dict[str, tuple[str, ...]] = {
    "auth login": AUTH_LOGIN_MARKERS,
    "auth logout": AUTH_LOGOUT_MARKERS,
    "auth whoami": AUTH_WHOAMI_MARKERS,
    "auth status": AUTH_STATUS_MARKERS,
    "repo create": REPO_MARKERS,
    "repo delete": REPO_MARKERS,
    "repo list": REPO_MARKERS,
    "ls": TRANSFER_MARKERS,
    "download": TRANSFER_MARKERS,
    "upload": TRANSFER_MARKERS,
}

# Auth-app and repo-app markers (the `omc auth --help` / `omc repo --help`
# intermediate screens).
APP_MARKERS: dict[str, tuple[str, ...]] = {
    "auth": AUTH_APP_MARKERS,
    "repo": REPO_APP_MARKERS,
}


__all__ = [
    "APP_MARKERS",
    "AUTH_APP_HELP",
    "AUTH_LOGIN_EPILOG",
    "AUTH_LOGOUT_EPILOG",
    "AUTH_STATUS_EPILOG",
    "AUTH_WHOAMI_EPILOG",
    "COMMAND_INDEX_MARKER",
    "COMMAND_MARKERS",
    "COMMON_EXIT_CODES",
    "CONCEPTS_MARKER",
    "CONFIG_MARKER",
    "CREDENTIAL_RESOLUTION_ORDER",
    "DOWNLOAD_EPILOG",
    "ENVIRONMENT_VARIABLES_MARKER",
    "EXAMPLES_MARKER",
    "EXIT_CODES_MARKER",
    "GETTING_STARTED_MARKER",
    "LS_EPILOG",
    "MORE_HELP_MARKER",
    "NOTES_MARKER",
    "PURPOSE_MARKER",
    "REPO_APP_HELP",
    "REPO_CREATE_EPILOG",
    "REPO_DELETE_EPILOG",
    "REPO_LIST_EPILOG",
    "ROOT_HELP",
    "ROOT_MARKERS",
    "SERVER_RESOLUTION_ORDER",
    "TRANSFER_MARKERS",
    "UPLOAD_EPILOG",
    "USAGE_MARKER",
]
