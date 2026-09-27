"""Per-subcommand epilog blocks for `omc auth ...`.

Layout per command (one file-level docstring above, four sections of
constants below): PURPOSE / USAGE / EXAMPLES / EXIT CODES / NOTES.

Each subcommand contributes:

    <CMD>_PURPOSE     - one-paragraph purpose
    <CMD>_USAGE       - invocation shape
    <CMD>_EXAMPLES    - 3-6 realistic invocations
    <CMD>_EXIT_CODES  - enumerated exit values + meanings
    <CMD>_NOTES       - auth requirements, config, server URL resolution
    <CMD>_EPILOG      - the assembled EXAMPLES + EXIT_CODES + NOTES block
                        wired into Typer's `epilog=` parameter

`COMMON_EXIT_CODES` (from `_common`) is concatenated into every
subcommand's `<CMD>_EXIT_CODES` so the 0/1/2 baseline is consistent.
"""

from __future__ import annotations

from outo_models_cli.commands._help_text._common import (
    COMMON_EXIT_CODES,
    CREDENTIAL_RESOLUTION_ORDER,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    NOTES_MARKER,
    PURPOSE_MARKER,
    SERVER_RESOLUTION_ORDER,
    USAGE_MARKER,
)

# Shared marker tuple — every auth subcommand's help shows the same five
# sections, so they all lock against the same marker list.
_AUTH_CMD_MARKERS: tuple[str, ...] = (
    PURPOSE_MARKER,
    USAGE_MARKER,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    NOTES_MARKER,
)

# --- login ----------------------------------------------------------------

AUTH_LOGIN_PURPOSE: str = (
    "PURPOSE\n"
    "  Verify a Personal Access Token against an outo-models server and\n"
    "  persist it under the normalized server URL so every other command\n"
    "  picks it up automatically. Use this once per server; the CLI will\n"
    "  remember it across shells, reboots, and rebooted shells."
)

AUTH_LOGIN_USAGE: str = (
    "USAGE\n"
    "  omc auth login --server <url> [--token PAT] [--set-default]\n"
    "  omc auth login                 # server taken from OMC_SERVER or the config default\n"
    "  omc auth login --token <PAT>   # PAT provided on the command line (scripts / CI)"
)

AUTH_LOGIN_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc auth login --server https://models.example.com\n"
    "      Interactive prompt for the PAT (input is masked). Token stored\n"
    "      after the server confirms it via `GET /api/auth/me`.\n"
    "\n"
    "  omc auth login --server https://models.example.com --token hf_xxx... --set-default\n"
    "      Non-interactive login for CI / scripted use. `--set-default` pins\n"
    "      the server as the default target for every subsequent command.\n"
    "\n"
    "  OMC_SERVER=https://models.example.com omc auth login --token hf_xxx...\n"
    "      Equivalent to passing `--server`; the env var wins over the\n"
    "      config default but loses to a `--server` flag on the same line.\n"
    "\n"
    "  omc auth login --server http://192.0.2.10:8080 --token hf_xxx...\n"
    "      Local / LAN server. The URL is normalized to `http://192.0.2.10:8080`\n"
    "      so subsequent commands can use either spelling.\n"
    "\n"
    "  # Then verify:\n"
    "  omc auth whoami\n"
    "  omc auth status"
)

AUTH_LOGIN_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Token verified and stored.\n"
        "  1  Verification or storage failed. Common reasons:\n"
        "       - empty PAT (the masked prompt returned an empty string)\n"
        "       - server returned 401 (typo, expired, or revoked PAT)\n"
        "       - 4xx/5xx with a JSON `detail` field - re-run with `--debug`.\n"
        "  2  Typer-level usage error (missing value, unknown flag)."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

AUTH_LOGIN_NOTES: str = (
    (
        "NOTES\n"
        "  - The PAT is stored at the path printed by `omc auth status`.\n"
        "  - `OMC_TOKEN` is honored but NEVER persisted: if the env var is set,\n"
        "    the CLI uses it for this invocation only and writes nothing to disk.\n"
        "  - The first `--server` ever logged into becomes the default; use\n"
        "    `--set-default` to change that at any time.\n"
        "  - Server URL normalization: trailing slashes stripped, scheme + host\n"
        "    lower-cased, `http://` prepended when no scheme is given.\n"
        "  - The CLI tightens the config file's mode to 0600 on every save."
    )
    + "\n"
    + SERVER_RESOLUTION_ORDER
)

AUTH_LOGIN_EPILOG: str = "\n\n".join(
    [AUTH_LOGIN_EXAMPLES, AUTH_LOGIN_EXIT_CODES, AUTH_LOGIN_NOTES],
)

# --- logout ---------------------------------------------------------------

AUTH_LOGOUT_PURPOSE: str = (
    "PURPOSE\n"
    "  Remove the stored credential for one server. Use this when you want\n"
    "  to revoke the CLI's access without rotating the PAT on the server\n"
    "  (the PAT itself is still valid until you revoke it server-side)."
)

AUTH_LOGOUT_USAGE: str = (
    "USAGE\n"
    "  omc auth logout [--server <url>]\n"
    "  omc auth logout                     # removes the resolved default server"
)

AUTH_LOGOUT_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc auth logout --server https://models.example.com\n"
    "      Removes the entry for that exact server (after normalization).\n"
    "\n"
    "  omc auth logout\n"
    "      Removes the entry for the default server (`omc auth status` to see it).\n"
    "\n"
    "  OMC_SERVER=https://models.example.com omc auth logout\n"
    "      Same effect as the first example; the env var supplies the URL.\n"
    "\n"
    "  # Confirm the credential is gone:\n"
    "  omc auth status"
)

AUTH_LOGOUT_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Credential removed (or already absent - re-running is a no-op).\n"
        "  1  No server was specified and none can be resolved from the env or\n"
        "     the config; or the resolved server has no stored credential.\n"
        "  2  Typer-level usage error."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

AUTH_LOGOUT_NOTES: str = (
    (
        "NOTES\n"
        "  - Logout is local-only: the server-side token is unaffected. Rotate\n"
        "    or revoke the PAT on the server to invalidate it elsewhere.\n"
        "  - If you log out of the default server, the next `auth login`\n"
        "    (or any remaining server in the config) becomes the default."
    )
    + "\n"
    + SERVER_RESOLUTION_ORDER
)

AUTH_LOGOUT_EPILOG: str = "\n\n".join(
    [AUTH_LOGOUT_EXAMPLES, AUTH_LOGOUT_EXIT_CODES, AUTH_LOGOUT_NOTES],
)

# --- whoami ---------------------------------------------------------------

AUTH_WHOAMI_PURPOSE: str = (
    "PURPOSE\n"
    "  Print the authenticated user, the resolved server, the server-side\n"
    "  role, and the source of the bearer token. Use it to confirm that a\n"
    "  new credential is wired up correctly before running mutating commands."
)

AUTH_WHOAMI_USAGE: str = "USAGE\n  omc auth whoami [--server <url>]"

AUTH_WHOAMI_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc auth whoami\n"
    "      Prints `server`, `username`, `role`, and `token source` for the\n"
    "      default server. Token source is `OMC_TOKEN env var` or\n"
    "      `stored credential` - the actual PAT is never printed.\n"
    "\n"
    "  omc auth whoami --server https://staging.example.com\n"
    "      Same, against an alternate server that has its own stored PAT.\n"
    "\n"
    "  OMC_TOKEN=... omc auth whoami\n"
    "      The token source line reads `OMC_TOKEN env var` so a CI log makes\n"
    "      the override explicit without leaking the value."
)

AUTH_WHOAMI_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Identity printed.\n"
        "  1  No credential for the resolved server (`auth_required`), or the\n"
        "     server rejected the PAT (`auth_invalid`, HTTP 401), or the\n"
        "     server is unreachable.\n"
        "  2  Typer-level usage error."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

AUTH_WHOAMI_NOTES: str = (
    (
        "NOTES\n"
        "  - The actual PAT is NEVER printed; only the source label.\n"
        "  - `role` is whatever the server reports (typically `user`,\n"
        "    `moderator`, or `admin`). It is informational, not enforced by\n"
        "    the CLI - the server enforces permissions.\n"
        "  - If the credential is missing, the command does not silently fall\n"
        "    back to another server; it exits 1."
    )
    + "\n"
    + SERVER_RESOLUTION_ORDER
    + "\n"
    + CREDENTIAL_RESOLUTION_ORDER
)

AUTH_WHOAMI_EPILOG: str = "\n\n".join(
    [AUTH_WHOAMI_EXAMPLES, AUTH_WHOAMI_EXIT_CODES, AUTH_WHOAMI_NOTES],
)

# --- status ---------------------------------------------------------------

AUTH_STATUS_PURPOSE: str = (
    "PURPOSE\n"
    "  List every server the CLI has a credential for and mark the default.\n"
    "  Pure local inspection - no network call, no token printed."
)

AUTH_STATUS_USAGE: str = "USAGE\n  omc auth status"

AUTH_STATUS_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc auth status\n"
    "      Prints a table of every server URL with a `yes` marker on the\n"
    "      default. If the table is empty, the next step is\n"
    "      `omc auth login --server <url>`.\n"
    "\n"
    "  # After logging into multiple servers:\n"
    "  omc auth status\n"
    "      # Server                              Default\n"
    "      # https://models.example.com           yes\n"
    "      # https://staging.example.com"
)

AUTH_STATUS_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Status table printed (or the empty hint when no servers are\n"
        "     configured - still a successful invocation)."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

AUTH_STATUS_NOTES: str = (
    "NOTES\n"
    "  - No flags. Reads only the local config file.\n"
    "  - The PAT itself is never displayed - only the URLs and the\n"
    "    default marker."
)

AUTH_STATUS_EPILOG: str = "\n\n".join(
    [AUTH_STATUS_EXAMPLES, AUTH_STATUS_EXIT_CODES, AUTH_STATUS_NOTES],
)


__all__ = [
    "AUTH_LOGIN_EPILOG",
    "AUTH_LOGIN_MARKERS",
    "AUTH_LOGOUT_EPILOG",
    "AUTH_LOGOUT_MARKERS",
    "AUTH_STATUS_EPILOG",
    "AUTH_STATUS_MARKERS",
    "AUTH_WHOAMI_EPILOG",
    "AUTH_WHOAMI_MARKERS",
]


# Per-subcommand marker tuples (all share the same five-section layout).
AUTH_LOGIN_MARKERS: tuple[str, ...] = _AUTH_CMD_MARKERS
AUTH_LOGOUT_MARKERS: tuple[str, ...] = _AUTH_CMD_MARKERS
AUTH_WHOAMI_MARKERS: tuple[str, ...] = _AUTH_CMD_MARKERS
AUTH_STATUS_MARKERS: tuple[str, ...] = _AUTH_CMD_MARKERS
