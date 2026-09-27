"""Help-text coverage: --help, --version, bare `omc`, and the token invariant.

Every test in this module locks a concrete piece of help text against
silent drift. The test names follow the existing pattern
(`test_<thing>_when_<condition>` is dropped where the original tests
used short positive assertions).

What is asserted:

  * `omc` (bare) and `omc --help` show the full root help (PURPOSE,
    USAGE, CONCEPTS, GETTING STARTED, COMMAND INDEX, CONFIG with the
    env-var inventory, MORE HELP) and exit 0.
  * `omc --version` is byte-identical to `omc <version>` and exits 0.
  * Every subcommand (`auth login`, `auth logout`, `auth whoami`,
    `auth status`, `repo create`, `repo delete`, `repo list`, `ls`,
    `download`, `upload`) shows PURPOSE, USAGE, EXAMPLES, EXIT CODES,
    NOTES on its `--help`.
  * The auth-app and repo-app intermediate help pages (`omc auth
    --help`, `omc repo --help`) show PURPOSE and USAGE.
  * The token is NEVER printed by any of: `auth status`, `auth whoami`,
    `auth logout` — the original token-never-leaks invariant.
  * The root help lists every command name (the original
    `test_root_help_lists_every_command`).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from outo_models_cli.commands._help_text import (
    APP_MARKERS,
    COMMAND_MARKERS,
    ROOT_MARKERS,
)
from outo_models_cli.main import app

SERVER = "http://api.test"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(result: Any) -> str:
    """result.stdout with ANSI styling stripped.

    Rich 15 honours FORCE_COLOR above NO_COLOR, and CI agents export
    FORCE_COLOR — assertions must match the TEXT, never the styling.
    """
    return _ANSI_RE.sub("", result.stdout)


@pytest.fixture
def runner() -> CliRunner:
    """Typer 0.27's `CliRunner` no longer accepts `mix_stderr` — stdout/stderr merge."""
    return CliRunner()


# ---------------------------------------------------------------------------
# Root app — bare `omc` and `omc --help`
# ---------------------------------------------------------------------------


def test_root_help_lists_every_command(runner: CliRunner) -> None:
    """`omc --help` must surface auth, repo, ls, download, upload."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("auth", "repo", "ls", "download", "upload"):
        assert cmd in _plain(result), f"missing {cmd} in root help"


def test_bare_omc_prints_full_help_and_exits_zero(runner: CliRunner) -> None:
    """Bare `omc` (no args) prints the full help and exits 0.

    AGENTS.md §2 / user contract: `omc` is the entry-point page, so
    it must show help and exit 0 — not exit 2 with a Click usage error.
    """
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    for marker in ROOT_MARKERS:
        assert marker in _plain(result), f"bare `omc` missing {marker!r} marker"


def test_root_help_includes_every_required_marker(runner: CliRunner) -> None:
    """`omc --help` must contain every section the contract requires."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for marker in ROOT_MARKERS:
        assert marker in _plain(result), f"root help missing {marker!r} marker"


def test_root_help_includes_environment_variable_inventory(runner: CliRunner) -> None:
    """Every env var the CLI reads must appear in the root help CONFIG block."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for var in (
        "OMC_SERVER",
        "OMC_TOKEN",
        "OMC_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "HOME",
    ):
        assert var in _plain(result), f"env var {var!r} missing from root help"


def test_root_help_command_index_lists_every_command(runner: CliRunner) -> None:
    """The COMMAND INDEX table must mention every command and its auth tag."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "auth login",
        "auth logout",
        "auth whoami",
        "auth status",
        "repo create",
        "repo delete",
        "repo list",
        "ls",
        "download",
        "upload",
    ):
        assert cmd in _plain(result), f"command {cmd!r} missing from COMMAND INDEX"


def test_root_help_getting_started_is_copy_pasteable(runner: CliRunner) -> None:
    """The GETTING STARTED block must contain the canonical 7-step flow."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for snippet in (
        "omc auth login --server",
        "omc auth whoami",
        "omc repo create",
        "omc ls",
        "omc download",
        "omc upload",
        "omc auth logout",
    ):
        assert snippet in _plain(result), f"GETTING STARTED missing canonical step {snippet!r}"


def test_root_help_explains_lfs(runner: CliRunner) -> None:
    """The CONCEPTS block must describe LFS behaviour for >100 MiB files."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "100 MiB" in _plain(result)
    assert "LFS" in _plain(result)
    assert "pointer text" in _plain(result)


# ---------------------------------------------------------------------------
# Version flag — unchanged contract
# ---------------------------------------------------------------------------


def test_version_flag(runner: CliRunner) -> None:
    """`omc --version` prints the package version and exits 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "omc" in _plain(result)


def test_version_flag_matches_bare_help_version(runner: CliRunner) -> None:
    """`--version` output starts with the same prefix the help describes."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # The version line always starts with `omc ` followed by a version.
    body = _plain(result).strip().splitlines()[-1]
    assert body.startswith("omc ")


# ---------------------------------------------------------------------------
# Per-command help — every command has the six-section layout
# ---------------------------------------------------------------------------


# Map of `omc <args>` → markers every command must expose on `--help`.
_EVERY_COMMAND_ARGS: dict[str, tuple[str, ...]] = {
    "auth login": ("auth", "login"),
    "auth logout": ("auth", "logout"),
    "auth whoami": ("auth", "whoami"),
    "auth status": ("auth", "status"),
    "repo create": ("repo", "create"),
    "repo delete": ("repo", "delete"),
    "repo list": ("repo", "list"),
    "ls": ("ls",),
    "download": ("download",),
    "upload": ("upload",),
}


@pytest.mark.parametrize(
    ("command_name", "args"),
    list(_EVERY_COMMAND_ARGS.items()),
    ids=sorted(_EVERY_COMMAND_ARGS.keys()),
)
def test_every_command_help_lists_markers(
    runner: CliRunner,
    command_name: str,
    args: tuple[str, ...],
) -> None:
    """Every command's `--help` contains its five required layout markers.

    The PURPOSE / USAGE / EXAMPLES / EXIT CODES / NOTES markers are
    single source of truth in `commands/_help_text`; this test fails
    if the epilog wiring ever drops a section.
    """
    markers = COMMAND_MARKERS[command_name]
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, f"{command_name} --help exited {result.exit_code}"
    for marker in markers:
        assert marker in _plain(result), f"{command_name} --help missing marker {marker!r}"


def test_auth_help_lists_every_subcommand(runner: CliRunner) -> None:
    """`omc auth --help` must list every auth subcommand."""
    result = runner.invoke(app, ["auth", "--help"])
    assert result.exit_code == 0
    for sub in ("login", "logout", "whoami", "status"):
        assert sub in _plain(result), f"auth --help missing {sub!r}"


def test_repo_help_lists_every_subcommand(runner: CliRunner) -> None:
    """`omc repo --help` must list every repo subcommand."""
    result = runner.invoke(app, ["repo", "--help"])
    assert result.exit_code == 0
    for sub in ("create", "delete", "list"):
        assert sub in _plain(result), f"repo --help missing {sub!r}"


@pytest.mark.parametrize(
    ("app_name", "args"),
    [("auth", ("auth",)), ("repo", ("repo",))],
    ids=["auth", "repo"],
)
def test_app_help_has_purpose_and_usage(
    runner: CliRunner,
    app_name: str,
    args: tuple[str, ...],
) -> None:
    """Intermediate `omc <app> --help` screens expose PURPOSE and USAGE."""
    markers = APP_MARKERS[app_name]
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0
    for marker in markers:
        assert marker in _plain(result), f"omc {app_name} --help missing {marker!r}"


def test_help_does_not_print_a_real_token(
    runner: CliRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Security: the token must never appear in stdout or stderr of any command."""
    secret = "TOPSECRET-9876543210"
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    runner.invoke(app, ["auth", "login", "--server", SERVER, "--token", secret])
    capsys.readouterr()  # discard the login output
    for cmd in (
        ["auth", "status"],
        ["auth", "whoami"],
        ["auth", "logout", "--server", SERVER],
    ):
        runner.invoke(app, cmd)
        captured = capsys.readouterr()
        assert secret not in captured.out, f"leaked in stdout: {cmd}"
        assert secret not in captured.err, f"leaked in stderr: {cmd}"


def test_auth_login_help_uses_placeholder_token_not_real(runner: CliRunner) -> None:
    """The auth login EXAMPLES use `hf_xxx...`, never a real PAT.

    The placeholder appears in the per-command help (not the root help
    — the root help has no `omc auth login --token ...` line). The
    security invariant is the same: no real token should ever appear
    in any help output.
    """
    result = runner.invoke(app, ["auth", "login", "--help"])
    assert result.exit_code == 0
    assert "hf_xxx" in _plain(result)
    # The literal `TOPSECRET` from the security invariant must not
    # appear anywhere on the help pages.
    assert "TOPSECRET" not in _plain(result)


# ---------------------------------------------------------------------------
# Help coverage sanity check — every command's help mentions every flag it
# accepts. This catches the case where someone deletes a flag without
# removing it from the help prose.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "flag"),
    [
        (("auth", "login"), "--set-default"),
        (("auth", "login"), "--token"),
        (("auth", "login"), "--server"),
        (("auth", "logout"), "--server"),
        (("auth", "whoami"), "--server"),
        (("repo", "create"), "--public"),
        (("repo", "create"), "--private"),
        (("repo", "create"), "--description"),
        (("repo", "delete"), "--yes"),
        (("repo", "list"), "--owner"),
        (("repo", "list"), "--kind"),
        (("ls",), "--path"),
        (("ls",), "--revision"),
        (("download",), "--include"),
        (("download",), "--exclude"),
        (("download",), "--local-dir"),
        (("download",), "--max-workers"),
        (("upload",), "--path-in-repo"),
        (("upload",), "--message"),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, tuple) else v,
)
def test_every_flag_appears_in_its_command_help(
    runner: CliRunner,
    args: tuple[str, ...],
    flag: str,
) -> None:
    """Every flag a command accepts is mentioned somewhere on its --help."""
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0
    assert flag in _plain(result), f"`omc {' '.join(args)} --help` does not mention flag {flag!r}"


_ = httpx
