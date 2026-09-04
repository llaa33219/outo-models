"""Tests for `outo-models update` — image-source precedence.

`update.py` picks the image reference in this order:

    1. `--image <ref>` on the CLI (normalized through `normalize_image_ref`).
    2. The `image` key in `/etc/outo-models/config.yaml` (the same file
       `start.py` reads).
    3. The fallback `ghcr.io/llaa33219/outo-models:stable` when neither is
       present.

The host-side `update.sh` script is invoked with that reference as its
single argument. We never run the script in tests — we capture the
argv via `stream_subprocess` and assert on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from outo_models.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def captured_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `stream_subprocess` so the test sees the argv the CLI built."""
    captured: list[str] = []

    def _fake(argv: list[str], env: dict[str, str] | None = None) -> int:
        captured.clear()
        captured.extend(argv)
        return 0

    monkeypatch.setattr("outo_models.cli.update.stream_subprocess", _fake)
    return captured


class TestUpdateImageFromCliFlag:
    """`--image` (when passed) wins; bare tags get the default registry."""

    def test_stable_track_normalized(
        self,
        runner: CliRunner,
        captured_argv: list[str],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(app, ["update", "--image", "stable"])
        assert result.exit_code == 0, result.output
        # argv = ["bash", <update.sh>, <image>]
        assert captured_argv[-1] == "ghcr.io/llaa33219/outo-models:stable"

    def test_dev_track_normalized(
        self,
        runner: CliRunner,
        captured_argv: list[str],
    ) -> None:
        result = runner.invoke(app, ["update", "--image", "dev"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "ghcr.io/llaa33219/outo-models:dev"

    def test_pinned_version_normalized(
        self,
        runner: CliRunner,
        captured_argv: list[str],
    ) -> None:
        result = runner.invoke(app, ["update", "--image", "0.2.0-stable"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "ghcr.io/llaa33219/outo-models:0.2.0-stable"

    def test_full_reference_passes_through(
        self,
        runner: CliRunner,
        captured_argv: list[str],
    ) -> None:
        result = runner.invoke(app, ["update", "--image", "localhost/outo-models:0.2.0-dev"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "localhost/outo-models:0.2.0-dev"

    def test_invalid_image_surfaces_validation_failed(
        self,
        runner: CliRunner,
        captured_argv: list[str],
    ) -> None:
        # `OutoError` is rendered as a single English line by the
        # `render_error` funnel, then exits 1.
        result = runner.invoke(app, ["update", "--image", "  "])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert captured_argv == []


class TestUpdateFallsBackToConfig:
    """Without `--image`, read the same `image` key `start.py` reads."""

    def test_reads_image_key_from_config(
        self,
        runner: CliRunner,
        captured_argv: list[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "image": "ghcr.io/some-fork/outo-models:0.2.0-stable",
                    "volume": "outo-models-data",
                    "ports": [80, 443],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "ghcr.io/some-fork/outo-models:0.2.0-stable"

    def test_missing_config_falls_back_to_stable(
        self,
        runner: CliRunner,
        captured_argv: list[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OUTO_CONFIG", str(tmp_path / "nonexistent.yaml"))

        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "ghcr.io/llaa33219/outo-models:stable"

    def test_config_missing_image_key_falls_back_to_stable(
        self,
        runner: CliRunner,
        captured_argv: list[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        config_path.write_text(
            yaml.safe_dump({"volume": "outo-models-data", "ports": [80, 443]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "ghcr.io/llaa33219/outo-models:stable"

    def test_malformed_config_falls_back_to_stable(
        self,
        runner: CliRunner,
        captured_argv: list[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        config_path.write_text("not: a: valid: yaml: at: all\n  - oops", encoding="utf-8")
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        assert captured_argv[-1] == "ghcr.io/llaa33219/outo-models:stable"


class TestUpdateScriptFailure:
    """A non-zero script exit is surfaced as `OutoError(code="update_failed")`."""

    def test_script_failure_exits_one(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _failing(argv: list[str], env: dict[str, str] | None = None) -> int:
            return 17

        monkeypatch.setattr("outo_models.cli.update.stream_subprocess", _failing)

        result = runner.invoke(app, ["update", "--image", "stable"])
        assert result.exit_code == 1
        assert "update_failed" in result.output
        assert "Traceback" not in result.output
