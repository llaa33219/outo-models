"""Tests for `outo-models setup` — interactive + non-interactive wizard.

Two surfaces:

    * `non_interactive=True` with all flags: deterministic — no prompts,
      asserts on the YAML + admin DB row.
    * Interactive surface: monkeypatches `cli.prompts.{text,password,confirm}`
      so we can drive the wizard through CliRunner without a real terminal.

The wizard must NEVER write the plaintext admin password anywhere (YAML,
DB row, log, console). The DB row must carry an argon2id hash; the YAML
must carry the API token only when the operator chose Cloudflare.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from outo_models.cli import prompts as cli_prompts
from outo_models.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def patched_prompts(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the prompt backend with deterministic canned answers.

    The text/password/choice helpers are queue-based: callers can push
    values before invoking the CLI. Defaults cover the common wizard
    walk where flags fill in the easy fields and only the image track
    choice + password + ports + require_approval actually need
    prompting.

    Prompt order with flags filling the easy fields: choice (image
    track) → text (public_ipv4) → password (admin_password) x2 → text
    (ports) → confirm (require_approval). The first prompt is now the
    image track (`_collect_image`), which runs before any other field.
    """
    import queue as _q

    choice_queue: _q.Queue[str] = _q.Queue()
    choice_queue.put("stable")

    text_queue: _q.Queue[str] = _q.Queue()
    # Order matches the wizard's prompt sequence when flags fill the
    # earlier fields: public_ipv4, then ports.
    text_queue.put("203.0.113.42")
    text_queue.put("80,443")

    password_queue: _q.Queue[str] = _q.Queue()
    password_queue.put("correct horse battery staple")
    password_queue.put("correct horse battery staple")

    def _text(*_a: Any, **_k: Any) -> str:
        return text_queue.get_nowait()

    def _pw(*_a: Any, **_k: Any) -> str:
        return password_queue.get_nowait()

    def _choice(*_a: Any, **_k: Any) -> str:
        return choice_queue.get_nowait()

    answers = {
        "text": _text,
        "password": _pw,
        "confirm": lambda *_a, **_k: True,
        "int_prompt": lambda *_a, **_k: 42,
        "choice": _choice,
    }
    for name, func in answers.items():
        monkeypatch.setattr(cli_prompts, name, func)
    return answers


# ---------------------------------------------------------------------------
# Non-interactive surface
# ---------------------------------------------------------------------------


class TestNonInteractive:
    """All flags → no prompts → deterministic YAML + admin DB row."""

    def test_full_non_interactive_writes_yaml_and_admin(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        patched_prompts: dict[str, Any],
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        # No DNS / firewall side-effects in unit tests — those are host-privileged.
        argv = [
            "setup",
            "run",
            "--non-interactive",
            "--domain",
            "models.example.com",
            "--acme-email",
            "ops@example.com",
            "--dns-provider",
            "manual",
            "--public-ipv4",
            "203.0.113.42",
            "--admin-username",
            "admin",
            "--admin-email",
            "admin@example.com",
            "--admin-password",
            "correct horse battery staple",
            "--skip-dns",
            "--skip-firewall",
            "--skip-ip-detect",
        ]
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output

        # YAML was written.
        assert config_path.exists()
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["domain"] == "models.example.com"
        assert payload["admin_username"] == "admin"
        assert payload["admin_email"] == "admin@example.com"
        assert payload["dns_provider"] == "manual"
        assert payload["public_ipv4"] == "203.0.113.42"
        # Secrets: no plaintext password in the YAML.
        raw = config_path.read_text(encoding="utf-8")
        assert "correct horse battery staple" not in raw
        # File mode is 0o600 — secrets inside, permissions locked down.
        mode = config_path.stat().st_mode & 0o777
        assert mode == 0o600

        # Admin user exists in the DB with role=admin / status=approved.
        import asyncio as _aio

        from sqlalchemy import select

        from outo_models.db import User, get_engine, get_session_factory

        async def _check() -> None:
            engine = get_engine(_settings())
            factory = get_session_factory(engine)
            async with factory() as session:
                rows = (await session.execute(select(User))).scalars().all()
                assert len(rows) == 1
                u = rows[0]
                assert u.username == "admin"
                assert u.email == "admin@example.com"
                assert u.role == "admin"
                assert u.status == "approved"
                assert u.password_hash.startswith("$argon2id$")
                assert "correct horse battery staple" not in u.password_hash
            await engine.dispose()

        _aio.run(_check())

    def test_missing_flag_in_non_interactive_mode_fails_cleanly(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        argv = [
            "setup",
            "run",
            "--non-interactive",
            "--domain",
            "models.example.com",
            # missing everything else
            "--skip-dns",
            "--skip-firewall",
        ]
        result = runner.invoke(app, argv)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "--non-interactive" in result.output or "required" in result.output
        assert not config_path.exists()

    def test_invalid_password_below_minimum(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        argv = [
            "setup",
            "run",
            "--non-interactive",
            "--domain",
            "models.example.com",
            "--acme-email",
            "ops@example.com",
            "--dns-provider",
            "manual",
            "--public-ipv4",
            "203.0.113.42",
            "--admin-username",
            "admin",
            "--admin-email",
            "admin@example.com",
            "--admin-password",
            "short",  # < 8 chars
            "--skip-dns",
            "--skip-firewall",
        ]
        result = runner.invoke(app, argv)
        assert result.exit_code == 1

    def test_image_flag_stable_track_writes_full_reference(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--public-ipv4",
                "203.0.113.42",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--image",
                "stable",
                "--skip-dns",
                "--skip-firewall",
            ],
        )
        assert result.exit_code == 0, result.output

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["image"] == "ghcr.io/llaa33219/outo-models:stable"

    def test_image_flag_full_reference_passes_through(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--public-ipv4",
                "203.0.113.42",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--image",
                "localhost/outo-models:0.2.0-dev",
                "--skip-dns",
                "--skip-firewall",
            ],
        )
        assert result.exit_code == 0, result.output

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["image"] == "localhost/outo-models:0.2.0-dev"

    def test_image_flag_omitted_defaults_to_stable_track(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--public-ipv4",
                "203.0.113.42",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-dns",
                "--skip-firewall",
            ],
        )
        assert result.exit_code == 0, result.output

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["image"] == "ghcr.io/llaa33219/outo-models:stable"

    def test_image_flag_invalid_value_fails_cleanly(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--public-ipv4",
                "203.0.113.42",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--image",
                "tag with space",
                "--skip-dns",
                "--skip-firewall",
            ],
        )
        assert result.exit_code == 1
        assert not config_path.exists()


# ---------------------------------------------------------------------------
# Interactive surface — monkeypatched prompts
# ---------------------------------------------------------------------------


class TestInteractive:
    """`--non-interactive=False` (default) drives `prompts.{text,password,...}`."""

    def test_interactive_wizard_completes_with_patched_prompts(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        patched_prompts: dict[str, Any],
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        # Patch the IP-detection `httpx.get` so the wizard doesn't try
        # to reach api.ipify.org from the test runner.
        import httpx as _httpx

        class _Resp:
            status_code = 200

            def __init__(self, text_value: str) -> None:
                self._text = text_value

            @property
            def text(self) -> str:
                return self._text

        monkeypatch.setattr(_httpx, "get", lambda *_a, **_k: _Resp("203.0.113.99"))

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--skip-dns",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
            input="correct horse battery staple\n",
        )
        assert result.exit_code == 0, result.output

        # Admin user exists.
        import asyncio as _aio

        from sqlalchemy import select

        from outo_models.db import User, get_engine, get_session_factory

        async def _check() -> None:
            engine = get_engine(_settings())
            factory = get_session_factory(engine)
            async with factory() as session:
                rows = (await session.execute(select(User))).scalars().all()
                assert any(u.username == "admin" and u.role == "admin" for u in rows)
            await engine.dispose()

        _aio.run(_check())

    def test_password_mismatch_reprompts(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        patched_prompts: dict[str, Any],
    ) -> None:
        # First two `password` calls are the wizard's pw / confirm pair;
        # if they differ, the loop re-asks. After two wrong + two right,
        # the wizard proceeds.
        calls: list[str] = []

        def _pw(_msg: str, **_k: object) -> str:
            calls.append(_msg)
            return "correct horse battery staple" if len(calls) >= 3 else "wrong"

        monkeypatch.setattr(cli_prompts, "password", _pw)
        monkeypatch.setattr(cli_prompts, "confirm", lambda *a, **k: True)

        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        import httpx as _httpx

        class _Resp:
            status_code = 200
            text = "203.0.113.99"

        monkeypatch.setattr(_httpx, "get", lambda *_a, **_k: _Resp())

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--skip-dns",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output
        # 4 prompt calls: pw1, pw2, pw3, pw4 (third and fourth match).
        assert len(calls) >= 4

    def test_image_track_prompt_runs_first(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The image track is the FIRST prompt — it frames every later
        # step. Verify the operator's choice (the dev track) is recorded
        # in the YAML as a full reference and echoed in the next-steps
        # banner.
        call_order: list[str] = []

        def _choice(_msg: str, **_k: object) -> str:
            call_order.append("choice")
            return "dev"

        def _text(_msg: str, **_k: object) -> str:
            call_order.append("text")
            return "203.0.113.42" if len(call_order) == 2 else "80,443"

        def _pw(_msg: str, **_k: object) -> str:
            call_order.append("password")
            return "correct horse battery staple"

        def _confirm(_msg: str, **_k: object) -> bool:
            call_order.append("confirm")
            return True

        monkeypatch.setattr(cli_prompts, "choice", _choice)
        monkeypatch.setattr(cli_prompts, "text", _text)
        monkeypatch.setattr(cli_prompts, "password", _pw)
        monkeypatch.setattr(cli_prompts, "confirm", _confirm)

        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        import httpx as _httpx

        class _Resp:
            status_code = 200
            text = "203.0.113.99"

        monkeypatch.setattr(_httpx, "get", lambda *_a, **_k: _Resp())

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--skip-dns",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output
        # `choice` was the first prompt; `dev` is normalized into a
        # full reference under the default registry.
        assert call_order[0] == "choice"
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["image"] == "ghcr.io/llaa33219/outo-models:dev"
        # Next-steps banner echoes the chosen image.
        assert "ghcr.io/llaa33219/outo-models:dev" in result.output

    def test_image_track_custom_prompts_for_reference(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Picking `custom` triggers a free-form text prompt; the value
        # is normalized through `normalize_image_ref`.
        choice_responses = iter(["custom"])
        text_responses = iter(
            [
                "localhost/outo-models:0.2.0-dev",
                "203.0.113.42",
                "80,443",
            ]
        )

        def _choice(_msg: str, **_k: object) -> str:
            return next(choice_responses)

        def _text(_msg: str, **_k: object) -> str:
            return next(text_responses)

        monkeypatch.setattr(cli_prompts, "choice", _choice)
        monkeypatch.setattr(cli_prompts, "text", _text)
        monkeypatch.setattr(cli_prompts, "password", lambda *a, **k: "correct horse battery staple")
        monkeypatch.setattr(cli_prompts, "confirm", lambda *a, **k: True)

        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        import httpx as _httpx

        class _Resp:
            status_code = 200
            text = "203.0.113.99"

        monkeypatch.setattr(_httpx, "get", lambda *_a, **_k: _Resp())

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--skip-dns",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["image"] == "localhost/outo-models:0.2.0-dev"


class TestBareSetupRunsWizard:
    """Bare `outo-models setup` (no subcommand) must run the wizard.

    The operator-facing form documented in README/install.md is `setup`,
    not `setup run` — the callback forwards to `setup run` with defaults.
    """

    def test_bare_setup_forwards_to_run_with_defaults(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import outo_models.cli.setup as setup_mod

        captured: dict[str, Any] = {}

        def _spy(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(setup_mod, "setup_run", _spy)
        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0, result.output
        # Defaults identical to `setup run` without any flags.
        assert captured == {
            "non_interactive": False,
            "domain": None,
            "acme_email": None,
            "dns_provider": None,
            "public_ipv4": None,
            "admin_username": None,
            "admin_email": None,
            "admin_password": None,
            "skip_dns": False,
            "skip_firewall": False,
            "skip_ip_detect": False,
            "yes": False,
            "ports": None,
            "require_approval": None,
            "image": None,
        }


# ---------------------------------------------------------------------------
# Internal / IP mode — domain is optional, ACME/DNS prompts are skipped
# ---------------------------------------------------------------------------


class TestInternalModeNonInteractive:
    """In internal mode (--domain empty or an IP), ACME / DNS prompts are skipped."""

    def test_ip_domain_skips_acme_and_dns(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "192.168.1.10",
                "--public-ipv4",
                "192.168.1.10",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["domain"] == "192.168.1.10"
        assert payload["dns_provider"] == "none"
        assert payload["acme_email"] == ""
        assert payload["public_ipv4"] == "192.168.1.10"

    def test_missing_domain_skips_acme_and_dns(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Omitting --domain entirely lands the wizard in internal mode;
        # the operator supplies --public-ipv4 to fill the address field.
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--public-ipv4",
                "10.0.0.5",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["domain"] == "10.0.0.5"
        assert payload["dns_provider"] == "none"
        assert payload["acme_email"] == ""

    def test_hostname_still_requires_acme_and_dns(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Hostname mode keeps the original requirement: --acme-email and
        # --dns-provider must be passed in non-interactive mode.
        config_path = tmp_path / "outo.yaml"
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "models.example.com",
                "--public-ipv4",
                "203.0.113.10",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 1
        assert "acme_email" in result.output or "required" in result.output


class TestInternalModeSkipsDnsStep:
    """When `SetupAnswers.is_internal`, the DNS step is skipped automatically."""

    def test_internal_mode_skips_ensure_dns_record(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        dns_calls = 0
        import outo_models.cli.setup._effect as effect_mod

        original = effect_mod.ensure_dns_record

        async def _spy(answers):
            nonlocal dns_calls
            dns_calls += 1
            await original(answers)

        monkeypatch.setattr(effect_mod, "ensure_dns_record", _spy)

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "192.168.1.10",
                "--public-ipv4",
                "192.168.1.10",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-firewall",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output
        assert dns_calls == 0


class TestFirewallContainerHostRequired:
    """When `open_ports` raises `firewall_container_host_required`, the wizard
    must print a warning and CONTINUE (the install is allowed to complete
    without firewall ports being opened from inside the container)."""

    def test_container_host_required_does_not_abort(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "outo.yaml"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("OUTO_DATA_DIR", str(data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))
        from outo_models.config import get_settings as _settings

        _settings.cache_clear()

        import outo_models.cli.setup._effect as effect_mod
        from outo_models.exceptions import OutoError

        async def _raise(*_a, **_kw):
            raise OutoError(
                "firewall tool not available in container; run "
                "/usr/local/share/outo-models/firewall-open.sh firewalld 80 443 on the host",
                code="firewall_container_host_required",
            )

        monkeypatch.setattr(effect_mod, "open_ports", _raise)

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "192.168.1.10",
                "--public-ipv4",
                "192.168.1.10",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 0, result.output
        assert config_path.exists()
        assert "/usr/local/share/outo-models/firewall-open.sh" in result.output

    def test_firewall_permission_still_aborts(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `firewall_permission` is a real misconfig (NOPASSWD missing);
        # the operator can fix it by re-running as root, so the wizard
        # surfaces a ConfigError and aborts. Only the
        # `firewall_container_host_required` branch is tolerant.
        config_path = tmp_path / "outo.yaml"
        monkeypatch.setenv("OUTO_CONFIG", str(config_path))

        import outo_models.cli.setup._effect as effect_mod
        from outo_models.exceptions import OutoError

        async def _raise(*_a, **_kw):
            raise OutoError(
                "firewall commands require elevated privileges",
                code="firewall_permission",
            )

        monkeypatch.setattr(effect_mod, "open_ports", _raise)

        result = runner.invoke(
            app,
            [
                "setup",
                "run",
                "--non-interactive",
                "--domain",
                "models.example.com",
                "--acme-email",
                "ops@example.com",
                "--dns-provider",
                "manual",
                "--public-ipv4",
                "203.0.113.10",
                "--admin-username",
                "admin",
                "--admin-email",
                "admin@example.com",
                "--admin-password",
                "correct horse battery staple",
                "--skip-ip-detect",
            ],
        )
        assert result.exit_code == 1
        # `write_config` runs before the firewall step, so a partially
        # written YAML is acceptable. The contract is that the wizard
        # surfaces the error and exits non-zero — only the
        # `firewall_container_host_required` branch is tolerant.
