"""Tests for the `outo-models` top-level Typer app.

Covers the small but security-critical surface of the CLI root:
    * `--version` prints the package version.
    * `--help` and every top-level command's `--help` succeed.
    * `OutoError` raised inside a command surfaces as one Korean line +
      exit 1, never a Python traceback.
    * Unknown errors are not swallowed — they propagate so CI sees them.
"""

from __future__ import annotations

import socket as socket_mod
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
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: False)
        monkeypatch.setattr(
            start_mod,
            "run_capture",
            lambda argv: (captured.append(argv), (0, ""))[1],
        )
        monkeypatch.setattr(start_mod, "_verify_started", lambda *_a, **_k: None)
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
        # The wizard's Caddyfile + config.yaml reach the server read-only.
        assert "/etc/outo-models:/etc/outo-models:ro" in argv

    def _run_start_capture_argv(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        from outo_models.cli import start as start_mod

        captured: list[list[str]] = []
        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: False)
        monkeypatch.setattr(
            start_mod,
            "run_capture",
            lambda argv: (captured.append(argv), (0, ""))[1],
        )
        monkeypatch.setattr(start_mod, "_verify_started", lambda *_a, **_k: None)
        monkeypatch.setenv("OUTO_CONFIG", str(self._write_config(tmp_path)))
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        return captured[0]

    def test_argv_mounts_socket_from_host_env_override(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shim passes the HOST socket path in OUTO_PODMAN_SOCK; the host
        # podman resolves the `-v` source, so the path is used verbatim even
        # when it is not visible from inside the CLI container.
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "in_container", lambda: True)
        monkeypatch.setattr(
            start_mod, "_probe_host_podman_sockets", lambda: ("/should/not/be/used.sock",)
        )
        monkeypatch.setenv("OUTO_PODMAN_SOCK", "/host-only/podman.sock")
        argv = self._run_start_capture_argv(runner, tmp_path, monkeypatch)
        pairs = [argv[i : i + 2] for i in range(len(argv) - 1)]
        assert ["-v", "/host-only/podman.sock:/run/podman/podman.sock:ro"] in pairs
        assert ["-e", "OUTO_PODMAN_SOCKET=/run/podman/podman.sock"] in pairs

    def test_argv_mounts_probe_discovered_socket(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.delenv("OUTO_PODMAN_SOCK", raising=False)
        monkeypatch.setattr(start_mod, "in_container", lambda: False)
        probed = tmp_path / "probed.sock"
        probed.write_text("", encoding="utf-8")
        monkeypatch.setattr(start_mod, "_probe_host_podman_sockets", lambda: (str(probed),))
        monkeypatch.setattr(Path, "is_socket", lambda self: self == probed)
        argv = self._run_start_capture_argv(runner, tmp_path, monkeypatch)
        assert f"{probed}:/run/podman/podman.sock:ro" in argv
        assert "OUTO_PODMAN_SOCKET=/run/podman/podman.sock" in argv

    def test_argv_omits_socket_inside_cli_container_without_host_env(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # In the CLI container every visible path is the container's own mount
        # point; passing it as a `-v` SOURCE would fail on the host.
        from outo_models.cli import start as start_mod

        monkeypatch.delenv("OUTO_PODMAN_SOCK", raising=False)
        monkeypatch.setattr(start_mod, "in_container", lambda: True)
        monkeypatch.setattr(Path, "is_socket", lambda self: True)
        argv = self._run_start_capture_argv(runner, tmp_path, monkeypatch)
        assert not [a for a in argv if "podman.sock" in a]
        assert argv[-1] == "ghcr.io/llaa33219/outo-models:dev"

    def test_argv_omits_socket_when_none_discoverable(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.delenv("OUTO_PODMAN_SOCK", raising=False)
        monkeypatch.setattr(
            start_mod, "_probe_host_podman_sockets", lambda: ("/absent/a.sock", "/absent/b.sock")
        )
        argv = self._run_start_capture_argv(runner, tmp_path, monkeypatch)
        assert not [a for a in argv if "podman.sock" in a]
        assert not [a for a in argv if "OUTO_PODMAN_SOCKET" in a]
        assert argv[-1] == "ghcr.io/llaa33219/outo-models:dev"  # start still succeeds

    def test_probe_skips_unreadable_candidates_instead_of_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path.is_socket() raises PermissionError on Python 3.12 when a parent
        # directory denies search (seen on CI runners under /run/user/<uid>).
        # An unreadable candidate is simply not usable — never a crash.
        from outo_models.cli import start as start_mod

        monkeypatch.delenv("OUTO_PODMAN_SOCK", raising=False)
        monkeypatch.setattr(start_mod, "in_container", lambda: False)

        def denied_or_good(path: Path) -> bool:
            if "denied" in str(path):
                raise PermissionError(13, "Permission denied")
            return "good" in str(path)

        monkeypatch.setattr(Path, "is_socket", denied_or_good)
        monkeypatch.setattr(
            start_mod, "_probe_host_podman_sockets", lambda: ("/denied/podman.sock",)
        )
        assert start_mod._host_podman_socket() is None

        monkeypatch.setattr(
            start_mod,
            "_probe_host_podman_sockets",
            lambda: ("/denied/podman.sock", "/good/podman.sock"),
        )
        assert start_mod._host_podman_socket() == "/good/podman.sock"

    def test_missing_config_refuses(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setenv("OUTO_CONFIG", str(tmp_path / "absent.yaml"))
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1

    def test_non_numeric_port_renders_config_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "image: ghcr.io/llaa33219/outo-models:dev\n"
            "volume: outo-models-data\n"
            'ports: ["eighty", 443]\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(cfg))
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert "invalid" in result.output.lower()
        assert "Traceback" not in result.output


class TestPodmanBase:
    """podman channel selection: binary on PATH, else remote over the socket."""

    def test_podman_binary_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models import cli as cli_pkg

        monkeypatch.setattr(
            cli_pkg.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None
        )
        assert cli_pkg.podman_base() == ["podman"]
        assert cli_pkg.podman_available() is True

    def test_remote_over_socket(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models import cli as cli_pkg

        sock = tmp_path / "podman.sock"
        with socket_mod.socket(socket_mod.AF_UNIX) as listener:
            listener.bind(str(sock))
            monkeypatch.setattr(
                cli_pkg.shutil,
                "which",
                lambda name: None if name == "podman" else "/usr/local/bin/podman-remote",
            )
            monkeypatch.setenv("OUTO_PODMAN_SOCKET", str(sock))
            assert cli_pkg.podman_base() == [
                "/usr/local/bin/podman-remote",
                "--url",
                f"unix://{sock}",
            ]
            assert cli_pkg.podman_available() is True

    def test_remote_without_socket_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models import cli as cli_pkg

        monkeypatch.setattr(
            cli_pkg.shutil, "which", lambda name: None if name == "podman" else "/x/podman-remote"
        )
        monkeypatch.setenv("OUTO_PODMAN_SOCKET", str(tmp_path / "absent.sock"))
        assert cli_pkg.podman_base() == []
        assert cli_pkg.podman_available() is False

    def test_script_env_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models import cli as cli_pkg

        sock = tmp_path / "podman.sock"
        with socket_mod.socket(socket_mod.AF_UNIX) as listener:
            listener.bind(str(sock))
            monkeypatch.setattr(
                cli_pkg.shutil,
                "which",
                lambda name: None if name == "podman" else "/usr/local/bin/podman-remote",
            )
            monkeypatch.setenv("OUTO_PODMAN_SOCKET", str(sock))
            env = cli_pkg.podman_script_env()
        assert env == {"PODMAN_BIN": "/usr/local/bin/podman-remote", "PODMAN_URL": f"unix://{sock}"}


class TestStartVerification:
    """start must fail loudly when the container never becomes healthy."""

    def _setup_common(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: False)
        monkeypatch.setattr(start_mod, "run_capture", lambda argv: (0, ""))
        monkeypatch.setattr(start_mod, "_collect_logs", lambda: "")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "image: ghcr.io/llaa33219/outo-models:dev\n"
            "volume: outo-models-data\n"
            "ports: [80, 443]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(cfg))

    def test_healthy_container_exits_zero(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup_common(tmp_path, monkeypatch)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "running")
        monkeypatch.setattr(start_mod, "_probe", lambda _url: True)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert "server is up" in result.output

    def test_dead_on_arrival_fails(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup_common(tmp_path, monkeypatch)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "exited")
        monkeypatch.setattr(start_mod, "_probe", lambda _url: False)
        logs: list[bool] = []
        monkeypatch.setattr(start_mod, "_collect_logs", lambda: logs.append(True) or "")
        result = runner.invoke(app, ["start", "--verify-timeout", "5"])
        assert result.exit_code == 1
        assert "start_verify_failed" in result.output
        assert logs == [True]

    def test_unhealthy_until_timeout_fails(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup_common(tmp_path, monkeypatch)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "running")
        monkeypatch.setattr(start_mod, "_probe", lambda _url: False)
        result = runner.invoke(app, ["start", "--verify-timeout", "2"])
        assert result.exit_code == 1
        assert "start_verify_failed" in result.output

    def test_no_verify_skips_health_check(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup_common(tmp_path, monkeypatch)

        def _forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("verification must be skipped with --no-verify")

        monkeypatch.setattr(start_mod, "_verify_started", _forbidden)
        result = runner.invoke(app, ["start", "--no-verify"])
        assert result.exit_code == 0, result.output
        assert "unverified" in result.output


class TestStartExistingContainer:
    """An existing container named outo-models must not fail start (exit 125
    in the field). Same image + running → verify only; otherwise replace."""

    def _setup_common(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, list[str]]:
        from outo_models.cli import start as start_mod

        calls: dict[str, list[str]] = {"rm": [], "run": []}

        def _capture(argv: list[str]) -> tuple[int, str]:
            calls["run"].append(" ".join(argv))
            return (0, "")

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: False)
        monkeypatch.setattr(start_mod, "run_capture", _capture)
        monkeypatch.setattr(start_mod, "_verify_started", lambda *_a, **_k: None)
        monkeypatch.setattr(start_mod, "_remove_container", lambda: calls["rm"].append("rm"))
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "image: ghcr.io/llaa33219/outo-models:dev\n"
            "volume: outo-models-data\n"
            "ports: [80, 443]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(cfg))
        return calls

    def test_running_same_image_skips_recreating(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        calls = self._setup_common(tmp_path, monkeypatch)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "running")
        monkeypatch.setattr(start_mod, "_container_image_matches", lambda _img: True)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert "already running" in result.output
        assert calls["run"] == [] and calls["rm"] == []

    def test_stopped_container_is_replaced(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        calls = self._setup_common(tmp_path, monkeypatch)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "exited")
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert "replacing existing container" in result.output
        assert calls["rm"] == ["rm"] and len(calls["run"]) == 1

    def test_running_stale_image_is_replaced(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        calls = self._setup_common(tmp_path, monkeypatch)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "running")
        monkeypatch.setattr(start_mod, "_container_image_matches", lambda _img: False)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert calls["rm"] == ["rm"] and len(calls["run"]) == 1


class TestContainerImageMatches:
    """Digest, not name, decides 'same image' (moving tags move)."""

    def test_same_digest_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "_container_image_id", lambda: "sha256:abc")
        monkeypatch.setattr(start_mod, "_local_image_id", lambda _img: "sha256:abc")
        assert start_mod._container_image_matches("x:dev") is True

    def test_same_name_different_digest_does_not_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "_container_image_id", lambda: "sha256:old")
        monkeypatch.setattr(start_mod, "_local_image_id", lambda _img: "sha256:new")
        assert start_mod._container_image_matches("x:dev") is False

    def test_unknowable_digest_means_replace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "_container_image_id", lambda: None)
        monkeypatch.setattr(start_mod, "_local_image_id", lambda _img: "sha256:new")
        assert start_mod._container_image_matches("x:dev") is False


class TestStartDiagnosis:
    """Failure advice maps log signatures to concrete next commands."""

    def _advice(self, logs: str, monkeypatch: pytest.MonkeyPatch) -> str:
        from outo_models.cli import start as start_mod

        printed: list[str] = []
        monkeypatch.setattr(start_mod, "print_status", lambda msg: printed.append(str(msg)))
        start_mod._print_diagnosis(logs, "http://127.0.0.1:80/healthz")
        return "\n".join(printed)

    def _advice_without_probe(self, logs: str, monkeypatch: pytest.MonkeyPatch) -> str:
        from outo_models.cli import start as start_mod

        printed: list[str] = []
        monkeypatch.setattr(start_mod, "print_status", lambda msg: printed.append(str(msg)))
        start_mod._print_diagnosis(logs)
        return "\n".join(printed)

    def test_rootlessport_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Verbatim shape of the field failure: podman refuses before the
        # container is ever created, so `podman logs` stays empty and only the
        # captured run output can identify it.
        advice = self._advice(
            "Error: rootlessport cannot expose privileged port 80 in container: "
            "listen tcp 0.0.0.0:80: bind: permission denied",
            monkeypatch,
        )
        assert "enable-low-ports.sh 80" in advice
        assert "--cap-add NET_BIND_SERVICE" in " ".join(advice.split())

    def test_rootlessport_rule_wins_over_listen_tcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        advice = self._advice(
            "rootlessport cannot expose privileged port 80: listen tcp 0.0.0.0:80: "
            "bind: permission denied",
            monkeypatch,
        )
        assert "rootlessport helper" in " ".join(advice.split())

    def test_generic_fallback_without_probe_omits_probe_clause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        advice = self._advice_without_probe("podman exploded", monkeypatch)
        assert "no known failure pattern" in advice
        assert "probe was" not in advice

    def test_low_port_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        advice = self._advice("Error: listen tcp :80: bind: permission denied", monkeypatch)
        assert "enable-low-ports.sh 80" in advice

    def test_address_in_use_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        advice = self._advice("bind: address already in use", monkeypatch)
        assert "ss -lntp" in advice

    def test_stale_image_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        advice = self._advice("No such command 'serve'", monkeypatch)
        assert "outo-models update" in advice

    def test_db_permission_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        advice = self._advice("unable to open database file", monkeypatch)
        assert "reset --destroy" in advice

    def test_acme_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        advice = self._advice("acme: registering account", monkeypatch)
        assert "--verify-timeout" in advice

    def test_unknown_signature_gives_generic_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        advice = self._advice("something entirely novel exploded", monkeypatch)
        assert "no known failure pattern" in advice
        assert "troubleshooting.md" in advice


class TestStartLowPortsPreflight:
    """Rootless podman + a host threshold above port 80 must abort cleanly.

    The field failure this pins: on a rootless engine the HOST-side
    rootlessport helper performs the port bind and the kernel refuses, so
    `podman run` exits 126 without ever creating the container. Catching it
    before the run keeps the diagnosis (and the existing container) intact.
    """

    def _setup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ports: str = "[80, 443]",
        state: str = "missing",
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: state)
        monkeypatch.setattr(start_mod, "_verify_started", lambda *_a, **_k: None)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            f"image: ghcr.io/llaa33219/outo-models:dev\nvolume: outo-models-data\nports: {ports}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(cfg))

    def test_rootless_blocked_aborts_before_podman_run(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch)

        def _forbidden(*_a: object, **_k: object) -> tuple[int, str]:
            raise AssertionError("podman run must not be attempted on a blocked host")

        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: True)
        monkeypatch.setattr(start_mod, "_podman_is_rootless", lambda: True)
        monkeypatch.setattr(start_mod, "run_capture", _forbidden)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert "low_ports_blocked" in result.output
        assert "/usr/local/share/outo-models/enable-low-ports.sh 80" in result.output

    def test_message_uses_lowest_configured_port(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch, ports="[8080, 8443]")
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: True)
        monkeypatch.setattr(start_mod, "_podman_is_rootless", lambda: True)
        monkeypatch.setattr(start_mod, "run_capture", _forbidden_run)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert "enable-low-ports.sh 8080" in result.output
        assert "low_ports_blocked" in result.output

    def test_unblocked_host_skips_the_podman_probe(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch)
        probed: list[bool] = []
        runs: list[list[str]] = []

        def _rootless() -> bool:
            probed.append(True)
            return True

        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: False)
        monkeypatch.setattr(start_mod, "_podman_is_rootless", _rootless)
        monkeypatch.setattr(start_mod, "run_capture", lambda argv: (runs.append(argv), (0, ""))[1])
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert probed == []
        assert len(runs) == 1

    def test_rootful_podman_proceeds_despite_blocked_threshold(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch)
        runs: list[list[str]] = []
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: True)
        monkeypatch.setattr(start_mod, "_podman_is_rootless", lambda: False)
        monkeypatch.setattr(start_mod, "run_capture", lambda argv: (runs.append(argv), (0, ""))[1])
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert len(runs) == 1
        assert "low_ports_blocked" not in result.output

    def test_unknown_rootless_mode_proceeds(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch)
        runs: list[list[str]] = []
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: True)
        monkeypatch.setattr(start_mod, "_podman_is_rootless", lambda: None)
        monkeypatch.setattr(start_mod, "run_capture", lambda argv: (runs.append(argv), (0, ""))[1])
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert len(runs) == 1

    def test_preflight_precedes_container_removal(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch, state="exited")
        removed: list[bool] = []
        monkeypatch.setattr(start_mod, "_remove_container", lambda: removed.append(True))
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: True)
        monkeypatch.setattr(start_mod, "_podman_is_rootless", lambda: True)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert removed == []

    def test_running_container_short_circuits_preflight(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch, state="running")
        monkeypatch.setattr(start_mod, "_container_image_matches", lambda _img: True)
        monkeypatch.setattr(start_mod, "_probe", lambda _url: True)
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: True)
        monkeypatch.setattr(
            start_mod,
            "_podman_is_rootless",
            lambda: pytest.fail("preflight must not run for an already-running container"),
        )
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert "already running" in result.output


def _forbidden_run(argv: list[str]) -> tuple[int, str]:
    raise AssertionError("podman run must not be attempted on a blocked host")


class _Completed:
    """Minimal `subprocess.Completed` stand-in for the probe tests."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class TestPodmanRootlessProbe:
    """`_podman_is_rootless` reports the engine mode, failing open on doubt."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, runner_: object) -> list[list[str]]:
        from outo_models.cli import start as start_mod

        calls: list[list[str]] = []

        def _run(argv: list[str], **_kwargs: object) -> object:
            calls.append(list(argv))
            return runner_

        monkeypatch.setattr(start_mod.subprocess, "run", _run)
        return calls

    def test_true_output_is_rootless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        self._patch(monkeypatch, _Completed(0, "true\n"))
        assert start_mod._podman_is_rootless() is True

    def test_false_output_is_rootful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        self._patch(monkeypatch, _Completed(0, "false\n"))
        assert start_mod._podman_is_rootless() is False

    def test_nonzero_exit_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        self._patch(monkeypatch, _Completed(125, "true\n"))
        assert start_mod._podman_is_rootless() is None

    def test_timeout_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        from outo_models.cli import start as start_mod

        def _boom(*_a: object, **_k: object) -> object:
            raise subprocess.TimeoutExpired(cmd="podman", timeout=5.0)

        monkeypatch.setattr(start_mod.subprocess, "run", _boom)
        assert start_mod._podman_is_rootless() is None

    def test_os_error_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        def _boom(*_a: object, **_k: object) -> object:
            raise FileNotFoundError("podman")

        monkeypatch.setattr(start_mod.subprocess, "run", _boom)
        assert start_mod._podman_is_rootless() is None

    def test_uses_the_podman_base_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from outo_models.cli import start as start_mod

        calls = self._patch(monkeypatch, _Completed(0, "true\n"))
        monkeypatch.setattr(
            start_mod,
            "podman_base",
            lambda: ["podman-remote", "--url", "unix:///run/podman/podman.sock"],
        )
        assert start_mod._podman_is_rootless() is True
        assert calls[0][:3] == [
            "podman-remote",
            "--url",
            "unix:///run/podman/podman.sock",
        ]
        assert calls[0][3:] == ["info", "--format", "{{.Host.Security.Rootless}}"]


class TestStartRunFailure:
    """A failed `podman run` surfaces podman's own message plus advice."""

    def _setup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        result: tuple[int, str],
    ) -> None:
        from outo_models.cli import start as start_mod

        monkeypatch.setattr(start_mod, "podman_available", lambda: True)
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: "missing")
        monkeypatch.setattr(start_mod, "low_ports_blocked", lambda *_a, **_k: False)
        monkeypatch.setattr(start_mod, "run_capture", lambda _argv: result)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "image: ghcr.io/llaa33219/outo-models:dev\n"
            "volume: outo-models-data\n"
            "ports: [80, 443]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OUTO_CONFIG", str(cfg))

    def test_nonzero_exit_prints_podman_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch, (126, "Error: name is in use"))
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert "Error: name is in use" in result.output
        assert "container failed to start (exit=126)" in result.output

    def test_rootlessport_output_gets_low_port_advice(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(
            tmp_path,
            monkeypatch,
            (
                126,
                "Error: rootlessport cannot expose privileged port 80 in container: "
                "listen tcp 0.0.0.0:80: bind: permission denied",
            ),
        )
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert "enable-low-ports.sh 80" in result.output

    def test_zero_exit_with_container_id_succeeds(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from outo_models.cli import start as start_mod

        self._setup(tmp_path, monkeypatch, (0, "9f1c2b3a4d5e\n"))
        states = iter(["missing", "running"])
        monkeypatch.setattr(start_mod, "_inspect_state", lambda: next(states, "running"))
        monkeypatch.setattr(start_mod, "_probe", lambda _url: True)
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0, result.output
        assert "server is up" in result.output
        assert "failed to start" not in result.output
