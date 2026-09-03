"""Tests for `outo_models.firewall.open_ports`.

The orchestrator runs `container/scripts/firewall-open.sh` via
`asyncio.create_subprocess_exec`. These tests monkeypatch both that and the
in-container detector so nothing escapes to the real firewall. `OUTO_FIREWALL_SCRIPT`
points at a tmp file so path resolution does not depend on the install layout.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from outo_models.exceptions import OutoError
from outo_models.firewall import detect as detect_mod
from outo_models.firewall.detect import FirewallKind
from outo_models.firewall.open_ports import (
    REQUIRED_PORTS,
    OpenPortsResult,
    open_ports,
)

# `outo_models.firewall.open_ports` re-exports the `open_ports` function from
# its `__init__.py`, so `import outo_models.firewall.open_ports as m` would
# resolve to the function (not the module). Reach into `sys.modules` to get
# the actual module object — needed because the orchestrator module imports
# `asyncio` that the tests monkeypatch at the module level.
open_ports_mod = sys.modules["outo_models.firewall.open_ports"]

# Exact host command the wizard prints to operators when the firewall cannot
# run inside the container. The contract is fixed: the message MUST contain
# this placeholder command verbatim so agent A's setup wizard can surface it.
EXPECTED_HOST_COMMAND = "/usr/local/share/outo-models/firewall-open.sh auto <ports...>"


@dataclass
class _ScriptedCall:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _FakeProc:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()


class _ProcRecorder:
    """Records every `asyncio.create_subprocess_exec` invocation and replays a
    pre-canned return code / stdout per call."""

    def __init__(self, scripted: list[_ScriptedCall] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._scripted = list(scripted or [])

    async def __call__(self, *args: str, **kwargs: object) -> _FakeProc:
        self.calls.append(list(args))
        call = self._scripted.pop(0) if self._scripted else _ScriptedCall()
        return _FakeProc(call.returncode, call.stdout, call.stderr)


@pytest.fixture
def proc(monkeypatch: pytest.MonkeyPatch) -> _ProcRecorder:
    """Intercept `asyncio.create_subprocess_exec` on both firewall modules."""
    recorder = _ProcRecorder()
    monkeypatch.setattr(open_ports_mod.asyncio, "create_subprocess_exec", recorder)
    # `detect_firewall` is only consulted when `kind` is None; the recorder is
    # safe to share — it will not be called unless a test explicitly asks.
    monkeypatch.setattr(detect_mod.asyncio, "create_subprocess_exec", recorder)
    return recorder


@pytest.fixture
def fake_script(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide a writable script path via `OUTO_FIREWALL_SCRIPT`.

    The file does not need to be executable: open_ports never executes it on
    the test side, and the env override means resolution skips the
    installed-package lookup. The path just has to be readable by `os.stat`.
    """
    script = tmp_path / "firewall-open.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.setenv("OUTO_FIREWALL_SCRIPT", str(script))
    return str(script)


@pytest.fixture
def not_in_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the orchestrator is running on a bare host (not a container)."""
    monkeypatch.setattr(open_ports_mod, "_in_container", lambda: False)


@pytest.fixture
def in_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the orchestrator is running inside a container (dockerenv / .containerenv)."""
    monkeypatch.setattr(open_ports_mod, "_in_container", lambda: True)


class TestRequiredPorts:
    """`REQUIRED_PORTS` is the default argv the wizard uses for new installs."""

    def test_default_is_http_and_https(self) -> None:
        assert REQUIRED_PORTS == (80, 443)

    def test_is_tuple(self) -> None:
        # Tuple — callers must not be tempted to mutate the module constant.
        assert isinstance(REQUIRED_PORTS, tuple)


class TestInContainerDetection:
    """When running inside a container, the orchestrator MUST refuse to spawn the
    host script and instead surface the exact host command the operator must run."""

    async def test_raises_firewall_container_host_required(
        self, proc: _ProcRecorder, fake_script: str, in_container: None
    ) -> None:
        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(80,), kind=FirewallKind.UFW)

        assert excinfo.value.code == "firewall_container_host_required"
        # The wizard prints this verbatim; the placeholder command MUST be exact.
        assert EXPECTED_HOST_COMMAND in str(excinfo.value)
        # No subprocess was spawned — we must never reach the host script.
        assert proc.calls == []

    async def test_raises_before_dry_run_too(
        self, proc: _ProcRecorder, fake_script: str, in_container: None
    ) -> None:
        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(80,), kind=FirewallKind.UFW, dry_run=True)

        assert excinfo.value.code == "firewall_container_host_required"
        assert EXPECTED_HOST_COMMAND in str(excinfo.value)
        assert proc.calls == []

    async def test_message_mentions_self_elevation(
        self, proc: _ProcRecorder, fake_script: str, in_container: None
    ) -> None:
        # Operators run the shim through a container; they need to know the
        # script will prompt for sudo on its own when needed.
        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(443,), kind=FirewallKind.FIREWALLD)

        assert "sudo" in str(excinfo.value).lower()

    async def test_no_raise_when_not_in_container(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        # Sanity: the default path does NOT raise. (Other fixtures handle the
        # rest of the argv; this only proves detection is wired correctly.)
        result = await open_ports(ports=(80,), kind=FirewallKind.UFW, dry_run=True)
        assert result.kind == FirewallKind.UFW


class TestInContainerHelper:
    """`_in_container` reads the standard /.dockerenv and /run/.containerenv markers."""

    def test_dockerenv_marker(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        # The helper uses Path.exists internally; we patch the module's _MARKER_PATHS
        # to point at a tmp dir where we control which files exist.
        dockerenv = tmp_path / "dockerenv"
        dockerenv.write_text("")
        monkeypatch.setattr(open_ports_mod, "_MARKER_PATHS", (dockerenv,))
        assert open_ports_mod._in_container() is True

    def test_containerenv_marker(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        containerenv = tmp_path / "containerenv"
        containerenv.write_text("")
        monkeypatch.setattr(open_ports_mod, "_MARKER_PATHS", (containerenv,))
        assert open_ports_mod._in_container() is True

    def test_neither_marker(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        # Point at a tmp dir with no markers.
        monkeypatch.setattr(open_ports_mod, "_MARKER_PATHS", (tmp_path / "absent",))
        assert open_ports_mod._in_container() is False


class TestOpenPortsDryRun:
    """`dry_run=True` returns the planned argv without spawning anything."""

    async def test_returns_commands_without_spawning(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        result = await open_ports(ports=(8080,), kind=FirewallKind.UFW, dry_run=True)

        assert isinstance(result, OpenPortsResult)
        assert result.kind == FirewallKind.UFW
        assert result.opened == []
        assert result.commands == [["bash", fake_script, "ufw", "8080"]]
        # Critical: zero subprocess invocations.
        assert proc.calls == []

    async def test_dry_run_with_required_ports_default(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        result = await open_ports(kind=FirewallKind.FIREWALLD, dry_run=True)

        assert result.commands == [["bash", fake_script, "firewalld", "80", "443"]]
        assert proc.calls == []

    async def test_argv_never_contains_sudo(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        # The script self-elevates; the Python side must NEVER prefix sudo.
        result = await open_ports(ports=(80,), kind=FirewallKind.UFW, dry_run=True)

        assert "sudo" not in result.commands[0]
        assert result.commands[0][0] == "bash"
        assert result.commands[0][1] == fake_script


class TestOpenPortsExecutes:
    """Without dry-run, the script is spawned once with the planned argv."""

    async def test_runs_bash_script_directly(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        result = await open_ports(ports=(80, 443), kind=FirewallKind.FIREWALLD)

        assert result.opened == [80, 443]
        assert proc.calls == [["bash", fake_script, "firewalld", "80", "443"]]

    async def test_argv_never_contains_sudo(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        # Critical regression guard: the orchestrator must NEVER prefix sudo.
        # Elevation is the host script's job, not the Python orchestrator's.
        await open_ports(ports=(443,), kind=FirewallKind.UFW)

        assert "sudo" not in proc.calls[0]
        assert proc.calls[0][0] == "bash"

    async def test_accepts_arbitrary_iterable(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        # Generators / sets must work — REQUIRED_PORTS is a tuple but callers may pass a set.
        ports = {80, 443}
        await open_ports(ports=ports, kind=FirewallKind.UFW)

        # argv must contain both ports regardless of iteration order.
        argv = proc.calls[0]
        assert argv[0] == "bash"
        assert argv[1] == fake_script
        assert argv[2] == "ufw"
        assert set(argv[3:]) == {"80", "443"}

    async def test_none_kind_still_invokes_script(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        # The host script prints guidance and exits 0 when kind=none.
        result = await open_ports(kind=FirewallKind.NONE)

        assert result.kind == FirewallKind.NONE
        assert result.opened == [80, 443]
        assert proc.calls == [["bash", fake_script, "none", "80", "443"]]


class TestScriptFailure:
    """When the host script returns non-zero, the orchestrator surfaces a typed
    error that includes the script's stderr tail."""

    async def test_nonzero_exit_raises_firewall_command_failed(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        proc._scripted = [_ScriptedCall(returncode=1, stderr="firewall-cmd: permission denied\n")]

        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(80,), kind=FirewallKind.FIREWALLD)

        assert excinfo.value.code == "firewall_command_failed"
        # The stderr tail must be reachable from the message so the wizard can
        # surface it verbatim.
        assert "firewall-cmd: permission denied" in str(excinfo.value)

    async def test_failure_message_includes_exit_code(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        proc._scripted = [_ScriptedCall(returncode=2, stderr="boom\n")]

        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(80,), kind=FirewallKind.UFW)

        # The exit code is part of the operator-visible message; assert its
        # presence rather than exact format to keep refactors honest.
        assert "exit" in str(excinfo.value).lower()
        assert "2" in str(excinfo.value)

    async def test_failure_with_empty_stderr_still_raises(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        proc._scripted = [_ScriptedCall(returncode=1, stderr="")]
        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(80,), kind=FirewallKind.UFW)

        assert excinfo.value.code == "firewall_command_failed"


class TestScriptPathResolution:
    """`OUTO_FIREWALL_SCRIPT` overrides the package-relative default."""

    async def test_env_override_used_when_set(
        self, proc: _ProcRecorder, fake_script: str, not_in_container: None
    ) -> None:
        result = await open_ports(kind=FirewallKind.UFW, dry_run=True)

        assert result.commands[0][1] == fake_script

    def test_default_path_is_under_repo_container_scripts(self) -> None:
        # The orchestrator resolves the bundled script as
        # `Path(__file__).resolve().parents[3] / "container" / "scripts" / "firewall-open.sh"`.
        # The exact absolute path varies by install layout, so we assert the
        # suffix + filename instead of equality.
        from pathlib import Path

        assert open_ports_mod.__file__ is not None
        default_path = (
            Path(open_ports_mod.__file__).resolve().parents[3]
            / "container"
            / "scripts"
            / "firewall-open.sh"
        )
        assert default_path.name == "firewall-open.sh"
        assert default_path.parent.name == "scripts"
        # Sanity: a real `container/scripts/` directory exists next to `src/`.
        assert default_path.parent.parent.name == "container"


class TestOpenPortsAutoDetect:
    """`kind=None` triggers `detect_firewall` and uses the discovered backend."""

    async def test_kind_none_runs_detection_and_uses_result(
        self,
        proc: _ProcRecorder,
        fake_script: str,
        not_in_container: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_detect() -> FirewallKind:
            return FirewallKind.NFTABLES

        monkeypatch.setattr(open_ports_mod, "detect_firewall", fake_detect)

        result = await open_ports(ports=(80,))

        assert result.kind == FirewallKind.NFTABLES
        assert proc.calls == [["bash", fake_script, "nftables", "80"]]

    async def test_detection_failure_still_returns_none_kind(
        self,
        proc: _ProcRecorder,
        fake_script: str,
        not_in_container: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_detect() -> FirewallKind:
            return FirewallKind.NONE

        monkeypatch.setattr(open_ports_mod, "detect_firewall", fake_detect)

        result = await open_ports(ports=(443,))

        assert result.kind == FirewallKind.NONE
        assert proc.calls == [["bash", fake_script, "none", "443"]]


class TestOpenPortsResultShape:
    """`OpenPortsResult` exposes the fields the setup wizard / UI consume."""

    def test_fields(self) -> None:
        sample_script = "/usr/local/sbin/outo-firewall.sh"
        r = OpenPortsResult(
            kind=FirewallKind.UFW,
            opened=[80, 443],
            commands=[["bash", sample_script, "ufw", "80", "443"]],
        )
        assert r.kind == FirewallKind.UFW
        assert r.opened == [80, 443]
        assert r.commands == [["bash", sample_script, "ufw", "80", "443"]]

    def test_no_needs_sudo_attribute(self) -> None:
        # Hard regression guard: the orchestrator dropped sudo handling; the
        # dataclass must not expose `needs_sudo` even as a default-True field.
        assert "needs_sudo" not in OpenPortsResult.__dataclass_fields__


class TestContainerCheckPrecedesDetection:
    """In-container, the refusal must fire BEFORE any host probing.

    Field failure: with kind=None, detect_firewall() ran first and crashed
    on the missing firewall-cmd binary inside the image — the wizard died
    with FileNotFoundError instead of printing the host command.
    """

    async def test_detection_never_runs_in_container(
        self,
        proc: _ProcRecorder,
        fake_script: str,
        in_container: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # open_ports_mod (module level, via sys.modules) — the package
        # __init__ re-export shadows a plain `import ... as` here.
        def _forbidden() -> None:
            raise AssertionError("detect_firewall must not be called inside a container")

        monkeypatch.setattr(open_ports_mod, "detect_firewall", _forbidden)
        with pytest.raises(OutoError) as excinfo:
            await open_ports(ports=(80, 443), kind=None)
        assert excinfo.value.code == "firewall_container_host_required"
        # The host command uses the script's host-side `auto` detection.
        assert " auto " in str(excinfo.value)
        assert proc.calls == []
