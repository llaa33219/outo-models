"""Help text for the `omc auth` sub-Typer itself (the intermediate screen).

The four subcommands' help lives in `_auth_cmds.py`; this module only
owns the body shown by `omc auth --help` (the parent Typer's help) and
the marker tuple the test suite uses to lock that body.
"""

from __future__ import annotations

from outo_models_cli.commands._help_text._common import (
    PURPOSE_MARKER,
    USAGE_MARKER,
)

AUTH_SUBCOMMAND_INDEX: str = (
    "AUTH SUBCOMMANDS\n"
    "  login    Verify a PAT against the server and store it for `--server`.\n"
    "  logout   Remove the stored credential for one server.\n"
    "  whoami   Print the authenticated user, server, role, and token source.\n"
    "  status   List every configured server and mark the default."
)

AUTH_APP_HELP: str = "\n\n".join(
    [
        "PURPOSE\n"
        "  Manage stored credentials: log in, log out, verify, inspect.\n"
        "  All four subcommands share the same server-resolution rules.",
        "USAGE\n  omc auth [SUBCOMMAND] [OPTIONS]",
        AUTH_SUBCOMMAND_INDEX,
        "Run `omc auth <subcommand> --help` for the full PURPOSE / EXAMPLES /\n"
        "EXIT CODES / NOTES reference of any individual subcommand.",
    ],
)

AUTH_APP_MARKERS: tuple[str, ...] = (PURPOSE_MARKER, USAGE_MARKER)


__all__ = ["AUTH_APP_HELP", "AUTH_APP_MARKERS", "AUTH_SUBCOMMAND_INDEX"]
