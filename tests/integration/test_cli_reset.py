"""Tests for `outo-models reset` — the destructive triple-yes gate.

These are the safety tests: every regression here is a regression of
AGENTS.md §2.2 ("any PR that changes the three-yes confirmation logic
or the dry-run-by-default behavior will be rejected"). The gate is
tested both through `CliRunner` (covers the real command surface) and
through the `_reset_impl` function (covers the dry-run / refusal / abort
branches the runner can't easily reach).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from outo_models.cli.main import app
from outo_models.cli.reset import (
    _DESTRUCTIVE_ENV,
    _REQUIRED_YES_COUNT,
    _YES_TOKEN,
    _gather_yes_confirmations,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


class TestResetDryRun:
    """The default is a dry run that touches nothing and exits 0."""

    def test_dry_run_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["reset"], env={"OUTO_CONFIG": str(tmp_path / "cfg.yaml")})
        assert result.exit_code == 0
        # Rich markup can be stripped by CliRunner — assert on plain text.
        assert "would be deleted" in result.output
        assert "volume: outo-models-data" in result.output
        assert not (tmp_path / "cfg.yaml").exists()

    def test_dry_run_lists_user_and_repo_counts(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-populate a data dir with a single user + repo file so the
        # dry-run summary has non-zero counts to render.
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()
        from outo_models.db import (
            Base,
            User,
            get_engine,
            get_session_factory,
        )

        async def _seed() -> None:
            engine = get_engine(_settings())
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = get_session_factory(engine)
            async with factory() as session:
                session.add(
                    User(
                        username="alice",
                        email="alice@example.com",
                        password_hash="x",
                        status="approved",
                    )
                )
                await session.commit()
            await engine.dispose()

        import asyncio as _aio

        _aio.run(_seed())
        _settings.cache_clear()

        # Drop a fake repo file under data/repos so total_bytes > 0.
        repos = data_dir / "repos" / "alice"
        repos.mkdir(parents=True, exist_ok=True)
        (repos / "blob").write_bytes(b"x" * 4096)

        result = runner.invoke(app, ["reset"])
        assert result.exit_code == 0, result.output
        assert "users: 1" in result.output
        assert "disk usage:" in result.output


# ---------------------------------------------------------------------------
# --destroy without env
# ---------------------------------------------------------------------------


class TestDestroyRefusesWithoutEnv:
    """`--destroy` requires `OUTO_DESTRUCTIVE=1`; without it, refuse + exit 1."""

    def test_destroy_without_env_refuses(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make sure no inherited env var slips through.
        monkeypatch.delenv(_DESTRUCTIVE_ENV, raising=False)
        monkeypatch.setenv("OUTO_DATA_DIR", str(tmp_path / "data"))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()
        result = runner.invoke(app, ["reset", "--destroy"], env={_DESTRUCTIVE_ENV: "0"})
        assert result.exit_code == 1
        # Clean Korean error — no traceback, no leaked secrets.
        assert "Traceback" not in result.output
        assert _DESTRUCTIVE_ENV in result.output


# ---------------------------------------------------------------------------
# env without --destroy
# ---------------------------------------------------------------------------


class TestEnvWithoutDestroyIsDryRun:
    """`OUTO_DESTRUCTIVE=1` alone (no `--destroy`) is still a dry run."""

    def test_env_without_destroy_is_dry_run(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_DESTRUCTIVE_ENV, "1")
        monkeypatch.setenv("OUTO_DATA_DIR", str(tmp_path / "data"))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()
        result = runner.invoke(app, ["reset"])
        assert result.exit_code == 0
        assert "dry-run" in result.output


# ---------------------------------------------------------------------------
# Triple-yes gate via `_gather_yes_confirmations`
# ---------------------------------------------------------------------------


class TestTripleYesGate:
    """The gate accepts exactly `yes`, repeated three times. Anything else aborts."""

    @pytest.mark.parametrize(
        "wrong_input",
        [
            "y",
            "Y",
            "YES",
            "yes ",  # trailing whitespace stripped by our check, but we test the raw
            "true",
            "",
            "n",
            "yess",
            " no",
        ],
    )
    def test_wrong_inputs_abort_on_first_prompt(
        self, wrong_input: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `input()` is what the gate calls; patch it to feed one wrong answer.
        answers = iter([wrong_input])
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
        assert _gather_yes_confirmations(0, 0, 0) is False

    def test_eof_aborts_safely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `input()` raises EOFError on a closed stream — the gate must
        # treat that as an abort, NOT as a default-to-yes surprise.
        def _raise_eof(*_a: object, **_k: object) -> None:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        assert _gather_yes_confirmations(0, 0, 0) is False

    def test_three_yes_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = iter([_YES_TOKEN] * _REQUIRED_YES_COUNT)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
        assert _gather_yes_confirmations(0, 0, 0) is True

    def test_partial_then_yes_still_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First answer wrong, then two yes — gate is dead, no carry-over."""
        answers = iter(["no", _YES_TOKEN, _YES_TOKEN])
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
        assert _gather_yes_confirmations(0, 0, 0) is False


# ---------------------------------------------------------------------------
# Full destroy flow with a fake reset.sh
# ---------------------------------------------------------------------------


class TestDestroyExecutes:
    """`--destroy` + env + three yes runs the script and wipes the data_dir."""

    def test_destroy_runs_fake_script_and_wipes_local_data(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Lay down a fake reset script under tmp_path; the CLI resolves it
        # via `OUTO_RESET_SCRIPT` (mirroring `OUTO_FIREWALL_SCRIPT`).
        fake_script = tmp_path / "reset.sh"
        marker = tmp_path / "reset_called"
        fake_script.write_text(f"#!/usr/bin/env bash\ntouch '{marker}'\nexit 0\n")
        fake_script.chmod(0o755)
        monkeypatch.setenv("OUTO_RESET_SCRIPT", str(fake_script))
        monkeypatch.setenv(_DESTRUCTIVE_ENV, "1")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        marker_in_data = data_dir / "secret"
        marker_in_data.write_bytes(b"do-not-leak")

        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        result = runner.invoke(
            app,
            ["reset", "--destroy"],
            input="\n".join([_YES_TOKEN] * _REQUIRED_YES_COUNT) + "\n",
        )
        assert result.exit_code == 0, result.output
        # Fake reset script was invoked.
        assert marker.exists()
        # Local data_dir was wiped.
        assert not marker_in_data.exists()
        assert not data_dir.exists()

    def test_destroy_wipes_config_dir_but_keeps_example(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Reset must return the machine to the first-install state: the
        # wizard's config.yaml + Caddyfile are operator state and go away;
        # the shipped config.example.yaml stays (it is not state).
        fake_script = tmp_path / "reset.sh"
        fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
        fake_script.chmod(0o755)
        monkeypatch.setenv("OUTO_RESET_SCRIPT", str(fake_script))
        monkeypatch.setenv(_DESTRUCTIVE_ENV, "1")

        config_dir = tmp_path / "etc"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("domain: 192.168.0.10\n")
        (config_dir / "Caddyfile").write_text(":80\n")
        (config_dir / "config.example.yaml").write_text("# shipped\n")
        monkeypatch.setenv("OUTO_CONFIG", str(config_dir / "config.yaml"))
        monkeypatch.setenv("OUTO_DATA_DIR", str(tmp_path / "absent-data"))

        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        result = runner.invoke(
            app,
            ["reset", "--destroy"],
            input="\n".join([_YES_TOKEN] * _REQUIRED_YES_COUNT) + "\n",
        )
        assert result.exit_code == 0, result.output
        assert not (config_dir / "config.yaml").exists()
        assert not (config_dir / "Caddyfile").exists()
        assert (config_dir / "config.example.yaml").exists()
        assert config_dir.is_dir()  # the shim bind-mounts this dir — keep it


# ---------------------------------------------------------------------------
# Sanity: required counts / tokens are constants the spec mandates
# ---------------------------------------------------------------------------


class TestInvariants:
    """Critical constants the spec mandates — don't let anyone weaken them."""

    def test_three_yes_count_is_three(self) -> None:
        assert _REQUIRED_YES_COUNT == 3

    def test_yes_token_is_lowercase_yes(self) -> None:
        assert _YES_TOKEN == "yes"


class TestGateWithUnknownCounts:
    """When the volume cannot be measured (destroy path via the shim mounts
    no volume), the gate must say ALL — never fabricated zeros."""

    def test_gate_messages_without_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = iter([_YES_TOKEN] * _REQUIRED_YES_COUNT)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
        assert _gather_yes_confirmations(None, None, None) is True

    def test_gate_messages_still_exact_with_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = iter([_YES_TOKEN] * _REQUIRED_YES_COUNT)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
        assert _gather_yes_confirmations(3, 2, 1024) is True


class TestLocalWipeSkipsImageDir:
    """Shim destroy path: the volume is unmounted, so data_dir is the
    image's own empty dir — wiping it must be skipped (EACCES in the field)."""

    def test_skips_when_container_and_no_state(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_script = tmp_path / "reset.sh"
        fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
        fake_script.chmod(0o755)
        monkeypatch.setenv("OUTO_RESET_SCRIPT", str(fake_script))
        monkeypatch.setenv(_DESTRUCTIVE_ENV, "1")

        image_like_dir = tmp_path / "image-data"
        image_like_dir.mkdir()
        (image_like_dir / "certs").mkdir()  # empty dirs only — no real state
        monkeypatch.setenv("OUTO_DATA_DIR", str(image_like_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(tmp_path / "etc" / "config.yaml"))
        monkeypatch.setattr("outo_models.cli.reset.in_container", lambda: True)

        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        result = runner.invoke(
            app,
            ["reset", "--destroy"],
            input="\n".join([_YES_TOKEN] * _REQUIRED_YES_COUNT) + "\n",
        )
        assert result.exit_code == 0, result.output
        # Untouched: nothing of ours was there, and the parent may be root-owned.
        assert image_like_dir.is_dir()
        assert (image_like_dir / "certs").is_dir()
