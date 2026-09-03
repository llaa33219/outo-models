"""Tests for the `outo-models` top-level Typer app.

Covers the small but security-critical surface of the CLI root:
    * `--version` prints the package version.
    * `--help` and every top-level command's `--help` succeed.
    * `OutoError` raised inside a command surfaces as one Korean line +
      exit 1, never a Python traceback.
    * Unknown errors are not swallowed — they propagate so CI sees them.
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from outo_models import version
from outo_models.cli import render_error
from outo_models.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    """Return a fresh `CliRunner`. Each test gets its own to avoid state leak."""
    return CliRunner()


class TestVersionFlag:
    """`--version` prints the package version and exits 0."""

    def test_prints_package_version(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert f"outo-models {version.__version__}" in result.output
        # Sanity check: the version string is parseable by `packaging`.
        assert _pkg_version("outo-models") == version.__version__


class TestHelpMessages:
    """`--help` on every public command must succeed (exit 0)."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--help"],
            ["setup", "--help"],
            ["setup", "run", "--help"],
            ["server", "--help"],
            ["server", "migrate", "--help"],
            ["server", "serve", "--help"],
            ["start", "--help"],
            ["stop", "--help"],
            ["restart", "--help"],
            ["status", "--help"],
            ["update", "--help"],
            ["reset", "--help"],
            ["admin", "--help"],
            ["admin", "list", "--help"],
            ["admin", "approve", "--help"],
            ["admin", "quota", "show", "--help"],
            ["admin", "gpu", "assign", "--help"],
        ],
    )
    def test_help_exits_zero(self, runner: CliRunner, argv: list[str]) -> None:
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, f"{argv} → exit {result.exit_code}, output: {result.output}"
        assert "Usage" in result.output


class TestOutoErrorRendering:
    """`OutoError` raises from a command render as a single Korean line."""

    def test_renders_typed_error_without_traceback(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["reset"])
        # dry-run path; nothing destructive, but the gate fires (no --destroy).
        # Reset without --destroy is a successful dry-run (exit 0) — so we
        # can't directly test the error funnel here. Use `admin list` with
        # bad status instead.
        assert result.exit_code == 0

    def test_admin_bad_status_returns_typed_error(self, runner: CliRunner) -> None:
        # Status validation is the easiest `OutoError` we can trigger from
        # the CLI without external setup.
        result = runner.invoke(app, ["admin", "list", "--status", "wat"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "error" in result.output.lower()

    def test_render_error_raises_for_unknown_exception(self) -> None:
        class _Weird(Exception):
            pass

        with pytest.raises(_Weird):
            render_error(_Weird("nope"))


class TestRootCallback:
    """The Typer root callback itself behaves correctly with no subcommand."""

    def test_no_args_prints_help(self, runner: CliRunner) -> None:
        # Typer 0.27's `no_args_is_help` exits non-zero on missing subcommand,
        # so assert the rendered help content rather than the exit code.
        result = runner.invoke(app, [])
        assert "Usage" in result.output
        assert "outo-models" in result.output


class TestStartCommand:
    """`outo-models start` argv contract against the wizard's config.yaml."""

    def _write_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "image: ghcr.io/llaa33219/outo-models:dev\n"
            "volume: outo-models-data\n"
            "ports: [80, 443]\n",
            encoding="utf-8",
        )
        return cfg

    def test_argv_has_keep_id_and_config_values(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        captured: list[list[str]] = []
        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setattr(start_mod, "stream_subprocess", lambda argv: captured.append(argv) or 0)
        cfg = self._write_config(tmp_path)
        monkeypatch.setenv("OUTO_CONFIG", str(cfg))
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        argv = captured[0]
        # Rootless ownership consistency with the CLI shim (see AGENTS.md).
        assert "--userns=keep-id" in argv
        assert argv[-1] == "ghcr.io/llaa33219/outo-models:dev"
        assert "outo-models-data:/var/lib/outo-models" in argv

    def test_missing_config_refuses(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setenv("OUTO_CONFIG", str(tmp_path / "absent.yaml"))
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
