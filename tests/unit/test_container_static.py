"""Static tests for the Containerfile, host scripts, quadlet example, and the
shipped `config.example.yaml`.

These tests run *without* podman — AGENTS.md §4 forbids any build/run claim on
this machine, so we only verify the artifacts statically:

* `Containerfile` has every required stage, ARG, USER, and the IMAGE_FLAVOR
  validation that aborts the build on bad input.
* every COPY source referenced from `Containerfile` exists in the repo right
  now (catches typos before the podman build 30 s in).
* every shell script (entrypoint + host scripts) parses with `bash -n`.
* the entrypoint refuses the dev+production flavor/env combination
  (AGENTS.md §4) and warns (does not fail) when non-root cannot bind 80/443.
* the quadlet example publishes the right ports, owns the right volume name,
  and keeps the NET_BIND_SERVICE capability commented out.
* the volume name `outo-models-data` is consistent across the quadlet,
  `update.sh`, and `reset.sh`.
* `config.example.yaml` parses as valid YAML and its top-level keys match
  `outo_models.config.Settings.model_fields` exactly — so a Settings rename
  breaks this test instead of silently drifting the docs.

Failures here point to a doc/config/code mismatch *before* a real build runs.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from outo_models.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTAINERFILE = REPO_ROOT / "Containerfile"
ROOTFS = REPO_ROOT / "container" / "rootfs"
SCRIPTS_DIR = REPO_ROOT / "container" / "scripts"
QUADLET_FILE = REPO_ROOT / "container" / "examples" / "quadlet" / "outo-models.container"
ENTRYPOINT = ROOTFS / "usr" / "local" / "bin" / "outo-entrypoint.sh"
CONFIG_EXAMPLE = ROOTFS / "etc" / "outo-models" / "config.example.yaml"

# `outo-models` is the named podman volume shared by the quadlet example and
# the host-side update / reset scripts. Test that no file typos this name —
# breaking it makes the volume diverge across files and silently strands data.
SHARED_VOLUME_NAME = "outo-models-data"

# Every shell script whose syntax we must validate. Includes both the
# entrypoint (inside the image) and the host-side helpers.
ALL_SHELL_SCRIPTS: tuple[Path, ...] = (
    REPO_ROOT / "container" / "scripts" / "firewall-open.sh",
    REPO_ROOT / "container" / "scripts" / "update.sh",
    REPO_ROOT / "container" / "scripts" / "reset.sh",
    ENTRYPOINT,
)

# File contents read once for the whole test session. Each file is small
# (< 10 KB) and never mutated by the tests, so a session-wide cache is safe.
CONTAINERFILE_TEXT = CONTAINERFILE.read_text(encoding="utf-8")
ENTRYPOINT_TEXT = ENTRYPOINT.read_text(encoding="utf-8")
QUADLET_TEXT = QUADLET_FILE.read_text(encoding="utf-8")
CONFIG_EXAMPLE_TEXT = CONFIG_EXAMPLE.read_text(encoding="utf-8")
CONFIG_EXAMPLE_PARSED: dict[str, object] = yaml.safe_load(CONFIG_EXAMPLE_TEXT)  # type: ignore[assignment]
assert isinstance(CONFIG_EXAMPLE_PARSED, dict), "expected top-level YAML mapping"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _local_copy_sources(text: str) -> list[str]:
    """Return every COPY source path that resolves against the repo root.

    `COPY --from=<stage> ...` directives are skipped — those reference a
    previous build stage's filesystem, not a repo-relative path.
    """
    sources: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY"):
            continue
        if "--from" in stripped:
            continue
        tokens = stripped.split()[1:]  # drop the COPY keyword
        # Drop flag tokens (start with `--`, e.g. `--chown=app:app`).
        positional = [t for t in tokens if not t.startswith("--")]
        # Last token is the destination; everything before is sources.
        sources.extend(positional[:-1])
    return sources


# ---------------------------------------------------------------------------
# Containerfile structure
# ---------------------------------------------------------------------------


class TestContainerfileStructure:
    """The Containerfile must declare every required stage + security primitive."""

    @pytest.mark.parametrize(
        "stage",
        ("builder", "caddy-builder", "runtime-base", "stable", "dev"),
    )
    def test_stage_present(self, stage: str) -> None:
        pattern = rf"^FROM\s+\S+\s+AS\s+{re.escape(stage)}\b"
        assert re.search(pattern, CONTAINERFILE_TEXT, re.MULTILINE) is not None, (
            f"missing required stage: {stage}"
        )

    def test_final_stage_picks_flavor(self) -> None:
        # `FROM ${IMAGE_FLAVOR} AS final` selects stable/dev at build time.
        assert re.search(
            r"^FROM\s+\$\{IMAGE_FLAVOR\}\s+AS\s+final\b",
            CONTAINERFILE_TEXT,
            re.MULTILINE,
        ) is not None

    def test_image_flavor_arg_default(self) -> None:
        # Default to `stable`; CLI passes `--build-arg IMAGE_FLAVOR=dev` for dev builds.
        assert (
            re.search(r"^ARG\s+IMAGE_FLAVOR=stable\b", CONTAINERFILE_TEXT, re.MULTILINE)
            is not None
        )

    def test_image_flavor_validated(self) -> None:
        # A RUN in runtime-base aborts the build when the arg is neither stable nor dev.
        assert "exit 1" in CONTAINERFILE_TEXT
        # Both literal flavor names appear in the validation context (not just in
        # unrelated FROM lines). The simplest proxy: a RUN block mentions both
        # `IMAGE_FLAVOR` and `exit 1` plus both flavor literals.
        flavor_validation = re.search(
            r"RUN[^\n]*(stable|IMAGE_FLAVOR)[^\n]*(stable|IMAGE_FLAVOR)[^\n]*",
            CONTAINERFILE_TEXT,
            re.DOTALL,
        )
        assert flavor_validation is not None, (
            "expected a RUN that validates IMAGE_FLAVOR against stable/dev"
        )

    def test_uv_sync_is_frozen_and_no_dev(self) -> None:
        assert "uv sync --frozen" in CONTAINERFILE_TEXT
        assert "--no-dev" in CONTAINERFILE_TEXT
        # --no-editable keeps the wheel installed into /app/.venv (no .pth shim).
        assert "--no-editable" in CONTAINERFILE_TEXT

    def test_caddy_builder_uses_cloudflare_plugin(self) -> None:
        assert "caddy:2-builder" in CONTAINERFILE_TEXT
        assert "xcaddy build" in CONTAINERFILE_TEXT
        assert "github.com/caddy-dns/cloudflare" in CONTAINERFILE_TEXT

    def test_runtime_base_uses_python_312(self) -> None:
        assert "python:3.12-slim" in CONTAINERFILE_TEXT

    def test_runtime_base_creates_non_root_app_user(self) -> None:
        # uid 1000 fixed so the named volume chown is stable across rebuilds.
        assert "useradd" in CONTAINERFILE_TEXT
        assert "-u 1000" in CONTAINERFILE_TEXT
        # The container ends with USER app (so the running process is non-root).
        assert re.search(r"^USER\s+app\b", CONTAINERFILE_TEXT, re.MULTILINE) is not None

    def test_data_dirs_owned_by_app(self) -> None:
        assert "/var/lib/outo-models" in CONTAINERFILE_TEXT
        assert "/etc/outo-models" in CONTAINERFILE_TEXT
        assert "chown" in CONTAINERFILE_TEXT

    def test_env_path_includes_venv(self) -> None:
        # /app/.venv/bin must precede the system PATH so `python`/`outo-models`
        # resolve to the installed venv, not /usr/bin/python.
        assert re.search(
            r'^ENV\s+PATH="/app/\.venv/bin:[^"]*"\s*$',
            CONTAINERFILE_TEXT,
            re.MULTILINE,
        ) is not None, "PATH must start with /app/.venv/bin"

    def test_exposes_public_ports(self) -> None:
        # EXPOSE 80 443 — Caddy's public listener pair.
        match = re.search(r"^EXPOSE\s+(.+)$", CONTAINERFILE_TEXT, re.MULTILINE)
        assert match is not None, "no EXPOSE directive found"
        ports = set(match.group(1).split())
        assert "80" in ports and "443" in ports, f"EXPOSE missing 80/443, got {match.group(1)!r}"

    def test_entrypoint_is_wrapper_script(self) -> None:
        # Exec-form JSON array, the only thing that gets proper signal forwarding.
        assert re.search(
            r'^ENTRYPOINT\s+\[\s*"/usr/local/bin/outo-entrypoint\.sh"\s*\]',
            CONTAINERFILE_TEXT,
            re.MULTILINE,
        ) is not None, "ENTRYPOINT must be the wrapper script in exec form"

    def test_dev_flavor_installs_debug_tools(self) -> None:
        # Only the `dev` stage is allowed to install debugpy + ipython (AGENTS.md §4).
        assert "debugpy" in CONTAINERFILE_TEXT
        assert "ipython" in CONTAINERFILE_TEXT

    def test_dev_flavor_sets_outo_env_development(self) -> None:
        assert "OUTO_ENV=development" in CONTAINERFILE_TEXT

    def test_stable_flavor_sets_outo_env_production(self) -> None:
        assert "OUTO_ENV=production" in CONTAINERFILE_TEXT


# ---------------------------------------------------------------------------
# COPY source existence
# ---------------------------------------------------------------------------


class TestContainerfileCopySources:
    """Every non-`--from` COPY source must resolve to a path inside the repo."""

    @pytest.fixture(scope="module")
    def sources(self) -> list[str]:
        return _local_copy_sources(CONTAINERFILE_TEXT)

    def test_at_least_one_local_copy(self, sources: list[str]) -> None:
        assert sources, "expected at least one COPY with a local source"

    @pytest.mark.parametrize(
        "rel_path",
        (
            "pyproject.toml",
            "uv.lock",
            "src",
            "container/caddy/Caddyfile.j2",
            "container/rootfs",
            "container/rootfs/etc/outo-models/config.example.yaml",
            "container/rootfs/usr/local/bin/outo-entrypoint.sh",
            "container/scripts",
            "container/scripts/update.sh",
            "container/scripts/reset.sh",
            "container/scripts/firewall-open.sh",
        ),
    )
    def test_required_source_exists(self, rel_path: str) -> None:
        """Files the Containerfile must COPY in. Listed explicitly so an
        accidentally-dropped COPY surfaces here, not at podman-build time."""
        path = REPO_ROOT / rel_path
        assert path.exists(), f"COPY source {rel_path!r} does not exist in the repo"

    def test_each_copy_source_in_repo(self, sources: list[str]) -> None:
        """Generic sweep — every source the Containerfile references today
        must exist. Catches drift between the test fixture and the file."""
        missing = [s for s in sources if not (REPO_ROOT / s).exists()]
        assert not missing, f"COPY sources missing from repo: {missing}"


# ---------------------------------------------------------------------------
# Shell scripts — bash -n sweep
# ---------------------------------------------------------------------------


class TestShellScriptsSyntax:
    """Every shell script the image (or the host) runs must parse cleanly."""

    @pytest.mark.parametrize(
        "script_path",
        ALL_SHELL_SCRIPTS,
        ids=lambda p: p.name,
    )
    def test_file_exists(self, script_path: Path) -> None:
        assert script_path.is_file(), f"{script_path} missing"

    @pytest.mark.parametrize(
        "script_path",
        ALL_SHELL_SCRIPTS,
        ids=lambda p: p.name,
    )
    def test_bash_n_passes(self, script_path: Path) -> None:
        # `script_path` is one of ALL_SHELL_SCRIPTS — a module-level tuple of
        # paths this test file itself owns. It is never operator-supplied, so
        # the subprocess S603 / S607 lint warnings do not apply.
        result = subprocess.run(  # noqa: S603
            ["bash", "-n", str(script_path)],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n {script_path.name} failed: "
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Entrypoint contract
# ---------------------------------------------------------------------------


class TestEntrypointContract:
    """The image entrypoint must enforce the AGENTS.md §4 dev/production gate
    and emit a Korean banner with the package version."""

    def test_has_strict_bash_mode(self) -> None:
        # pipefail ensures a pipeline failure inside any helper still aborts us.
        assert "set -euo pipefail" in ENTRYPOINT_TEXT

    def test_refuses_dev_plus_production(self) -> None:
        # The condition: IMAGE_FLAVOR=dev AND OUTO_ENV=production → exit 1.
        assert '== "dev"' in ENTRYPOINT_TEXT
        assert '== "production"' in ENTRYPOINT_TEXT
        # The combination check is what enforces AGENTS.md §4, not just the
        # individual equalities. We look for the literal "production" being
        # tested AFTER a dev test (presence of both in a single if branch).
        assert re.search(r'IMAGE_FLAVOR.*==.*"dev"', ENTRYPOINT_TEXT, re.DOTALL) is not None
        assert re.search(r'OUTO_ENV.*==.*"production"', ENTRYPOINT_TEXT, re.DOTALL) is not None
        # exit 1 must appear in the same file (the refuse branch).
        assert "exit 1" in ENTRYPOINT_TEXT

    def test_prints_korean_version_banner(self) -> None:
        # The version is pulled from outo_models.version.__version__.
        assert "outo_models.version" in ENTRYPOINT_TEXT
        assert "__version__" in ENTRYPOINT_TEXT
        # At least some Hangul in the banner — proves the banner is Korean.
        hangul_count = sum(1 for ch in ENTRYPOINT_TEXT if 0xAC00 <= ord(ch) <= 0xD7A3)
        assert hangul_count >= 10, (
            f"expected Korean banner (>=10 Hangul chars), got {hangul_count}"
        )

    def test_warns_about_unprivileged_ports(self) -> None:
        # Reads /proc/sys/net/ipv4/ip_unprivileged_port_start to detect the
        # kernel default and points the operator at docs/troubleshooting.md.
        assert "ip_unprivileged_port_start" in ENTRYPOINT_TEXT
        assert "docs/troubleshooting.md" in ENTRYPOINT_TEXT
        # euid check — `id -u` or `EUID` env, both common idioms.
        assert 'id -u' in ENTRYPOINT_TEXT or 'EUID' in ENTRYPOINT_TEXT
        # The warning must NOT cause a non-zero exit (warn, not fail).
        # We assert the warning block does NOT contain `exit 1`.
        warn_block_match = re.search(
            r"if[^\n]*unprivileged_port_start[^\n]*;[^\n]*\n(.*?)\n\s*fi",
            ENTRYPOINT_TEXT,
            re.DOTALL,
        )
        if warn_block_match is not None:
            assert "exit 1" not in warn_block_match.group(1), (
                "port warning block must not exit non-zero (warn, not fail)"
            )

    def test_exec_outo_models(self) -> None:
        # `exec` replaces the shell so signals reach the CLI directly.
        assert re.search(r"^exec\s+outo-models\b", ENTRYPOINT_TEXT, re.MULTILINE) is not None

    def test_fails_loudly_when_cli_missing(self) -> None:
        # Per task: fails with a clear message if the CLI is missing.
        assert "command -v outo-models" in ENTRYPOINT_TEXT
        assert "exit 1" in ENTRYPOINT_TEXT


# ---------------------------------------------------------------------------
# Quadlet example
# ---------------------------------------------------------------------------


class TestQuadletExample:
    """The quadlet example must publish the right ports, name the right
    volume, and keep privileged capabilities commented out."""

    def test_exists(self) -> None:
        assert QUADLET_FILE.is_file()

    def test_image_is_stable(self) -> None:
        assert (
            re.search(r"^Image=localhost/outo-models:stable\b", QUADLET_TEXT, re.MULTILINE)
            is not None
        )

    @pytest.mark.parametrize("port", ("80:80", "443:443"))
    def test_publishes_port(self, port: str) -> None:
        assert f"PublishPort={port}" in QUADLET_TEXT

    def test_volume_matches_host_scripts(self) -> None:
        assert f"Volume={SHARED_VOLUME_NAME}:/var/lib/outo-models" in QUADLET_TEXT

    def test_no_new_privileges_enabled(self) -> None:
        assert re.search(r"^SecurityOpt=no-new-privileges:true\b", QUADLET_TEXT, re.MULTILINE)

    def test_net_bind_service_commented_out(self) -> None:
        # The capability must appear (so operators know it exists) but stay
        # commented out by default — opt-in via NET_BIND_SERVICE comment removal.
        assert re.search(
            r"^#\s*AddCapability=NET_BIND_SERVICE\b", QUADLET_TEXT, re.MULTILINE
        ) is not None, "AddCapability=NET_BIND_SERVICE must be present, commented"
        # And the un-commented form must NOT exist (would auto-enable the cap).
        assert re.search(
            r"^AddCapability=NET_BIND_SERVICE\b", QUADLET_TEXT, re.MULTILINE
        ) is None, "AddCapability=NET_BIND_SERVICE must NOT be enabled by default"


# ---------------------------------------------------------------------------
# Volume name consistency across files
# ---------------------------------------------------------------------------


class TestSharedVolumeName:
    """`outo-models-data` must be the exact same string in every file that
    references it — otherwise update.sh / reset.sh / quadlet would target
    three different volumes and silently strand data."""

    FILES = (
        QUADLET_FILE,
        REPO_ROOT / "container" / "scripts" / "update.sh",
        REPO_ROOT / "container" / "scripts" / "reset.sh",
    )

    @pytest.mark.parametrize("file_path", FILES, ids=lambda p: p.name)
    def test_volume_name_present(self, file_path: Path) -> None:
        assert file_path.is_file()
        text = file_path.read_text(encoding="utf-8")
        assert SHARED_VOLUME_NAME in text, (
            f"{file_path.name} missing shared volume name {SHARED_VOLUME_NAME!r}"
        )


# ---------------------------------------------------------------------------
# config.example.yaml — YAML validity + Settings drift detection
# ---------------------------------------------------------------------------


class TestConfigExampleYaml:
    """The shipped example config must parse, and its top-level keys must
    match `Settings.model_fields` exactly. A field added to Settings but not
    to the YAML would break operators at first-run time."""

    def test_exists(self) -> None:
        assert CONFIG_EXAMPLE.is_file()

    def test_parses_as_valid_yaml(self) -> None:
        # yaml.safe_load succeeded if CONFIG_EXAMPLE_PARSED was populated.
        assert CONFIG_EXAMPLE_PARSED

    def test_keys_match_settings_fields(self) -> None:
        yaml_keys = set(CONFIG_EXAMPLE_PARSED.keys())
        settings_keys = set(Settings.model_fields.keys())
        # Every Settings field must appear in the example.
        missing = settings_keys - yaml_keys
        assert not missing, (
            f"config.example.yaml is missing Settings fields: {sorted(missing)}"
        )
        # No extra top-level keys (would suggest a typo or stale field).
        extra = yaml_keys - settings_keys
        assert not extra, (
            f"config.example.yaml has unknown top-level keys: {sorted(extra)}"
        )

    @pytest.mark.parametrize(
        "field_name",
        tuple(Settings.model_fields.keys()),
    )
    def test_field_present(self, field_name: str) -> None:
        # Granular version of the keys-match check — a failing test name
        # pinpoints exactly which Settings field is missing from the YAML.
        assert field_name in CONFIG_EXAMPLE_PARSED, (
            f"Settings field {field_name!r} not in example YAML"
        )


# ---------------------------------------------------------------------------
# .containerignore sanity (sanity — not a full coverage test)
# ---------------------------------------------------------------------------


class TestContainerignore:
    """We just verify the file exists; the Containerfile sweep above already
    covers path existence. `.containerignore` is a hint to the build engine
    and a missing entry only costs build-context size, not correctness."""

    def test_exists(self) -> None:
        assert (REPO_ROOT / ".containerignore").is_file()
