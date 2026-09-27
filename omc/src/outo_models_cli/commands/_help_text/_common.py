"""Common blocks shared across multiple commands' help text.

Every block in this module is referenced from at least two command help
bodies — concentrating them keeps the wording identical (no drift
between `auth login` and `auth logout` over the server-resolution rules,
for example) and keeps each `_help_text/*.py` file under the 250-LOC
ceiling.
"""

from __future__ import annotations

# Stable markers — the test suite asserts on these literals to lock the
# help layout. Keep them as bare identifiers (no rich markup) so a
# `grep MARKER omc --help` finds them in any TTY / non-TTY context.
PURPOSE_MARKER: str = "PURPOSE"
USAGE_MARKER: str = "USAGE"
EXAMPLES_MARKER: str = "EXAMPLES"
EXIT_CODES_MARKER: str = "EXIT CODES"
NOTES_MARKER: str = "NOTES"
GETTING_STARTED_MARKER: str = "GETTING STARTED"
COMMAND_INDEX_MARKER: str = "COMMAND INDEX"
CONCEPTS_MARKER: str = "CONCEPTS"
CONFIG_MARKER: str = "CONFIG"
MORE_HELP_MARKER: str = "MORE HELP"
ENVIRONMENT_VARIABLES_MARKER: str = "ENVIRONMENT VARIABLES"

# Server URL resolution order — referenced by every command that talks
# to a server. The test suite asserts on the literal string to catch
# silent reorderings.
SERVER_RESOLUTION_ORDER: str = (
    "Server URL resolution order (highest priority first):\n"
    "  1. --server <url> flag\n"
    "  2. OMC_SERVER environment variable\n"
    "  3. default_server in the config file (set by `omc auth login --set-default`)\n"
    "  4. the first server present in the config file (the very first `auth login` wins)"
)

# Credential resolution order — same precedence shape as the URL one.
CREDENTIAL_RESOLUTION_ORDER: str = (
    "Token resolution order (highest priority first):\n"
    "  1. OMC_TOKEN environment variable (used verbatim, never persisted)\n"
    "  2. the token stored for the resolved server in the config file"
)

# Notes about credential storage — used by every authenticated command.
AUTH_NOTES: str = (
    "AUTH\n"
    "  All authenticated commands need a PAT stored for the resolved server.\n"
    "  Run `omc auth login --server <url>` once; subsequent commands pick it up.\n"
    "  The PAT is stored at the path given by `omc auth status` and is never echoed."
)

# Common exit codes — every command emits these, plus any command-specific
# ones listed in its own block.
COMMON_EXIT_CODES: str = (
    "EXIT CODES\n"
    "  0  Success.\n"
    "  1  The command failed (network, auth, validation, file system).\n"
    "     The single English error line is written to stderr; re-run with\n"
    "     `--debug` to see the full Python traceback.\n"
    "  2  Typer-level usage error (unknown flag, missing required argument).\n"
    "     Re-run with `--help` for the canonical shape."
)


__all__ = [
    "AUTH_NOTES",
    "COMMAND_INDEX_MARKER",
    "COMMON_EXIT_CODES",
    "CONCEPTS_MARKER",
    "CONFIG_MARKER",
    "CREDENTIAL_RESOLUTION_ORDER",
    "ENVIRONMENT_VARIABLES_MARKER",
    "EXAMPLES_MARKER",
    "EXIT_CODES_MARKER",
    "GETTING_STARTED_MARKER",
    "MORE_HELP_MARKER",
    "NOTES_MARKER",
    "PURPOSE_MARKER",
    "SERVER_RESOLUTION_ORDER",
    "USAGE_MARKER",
]
