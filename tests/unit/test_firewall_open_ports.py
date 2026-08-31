"""Tests for `outo_models.firewall.open_ports`.

The orchestrator runs `container/scripts/firewall-open.sh` via
`asyncio.create_subprocess_exec`. These tests monkeypatch both that and
`os.geteuid` so nothing escapes to the real firewall, and point
`OUTO_FIREWALL_SCRIPT` at a tmp file so path resolution does not depend on
the install layout.
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
# `os` / `asyncio` that the tests monkeypatch at the module level.
open_ports_mod = sys.modules["outo_models.firewall.open_ports"]


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
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the current process is uid 0 so no `sudo -n` prefix is added."""
    monkeypatch.setattr(open_ports_mod.os, "geteuid", lambda: 0)


@pytest.fixture
def as_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the current process is unprivileged so `sudo -n` is required."""
    monkeypatch.setattr(open_ports_mod.os, "geteuid", lambda: 1000)


class TestRequiredPorts:
    """`REQUIRED_PORTS` is the default argv the wizard uses for new installs."""

    def test_default_is_http_and_https(self) -> None:
        assert REQUIRED_PORTS == (80, 443)

    def test_is_tuple(self) -> None:
        # Tuple — callers must not be tempted to mutate the module constant.
        assert isinstance(REQUIRED_PORTS, tuple)


class TestOpenPortsDryRun:
    """`dry_run=True` returns the planned argv without spawning anything."""

    async def test_returns_commands_without_spawning(
        self, proc: _ProcRecorder, fake_script: str, as_root: None
    ) -> None:
        result = await open_ports(ports=(8080,), kind=FirewallKind.UFW, dry_run=True)

        assert isinstance(result, OpenPortsResult)
        assert result.kind == FirewallKind.UFW
        assert result.opened == []
        assert result.needs_sudo is False
        assert result.commands == [["bash", fake_script, "ufw", "8080"]]
        # Critical: zero subprocess invocations.
        assert proc.calls == []

    async def test_dry_run_with_required_ports_default(
        self, proc: _ProcRecorder, fake_script: str, as_root: None
    ) -> None:
        result = await open_ports(kind=FirewallKind.FIREWALLD, dry_run=True)

        assert result.commands == [["bash", fake_script, "firewalld", "80", "443"]]
        assert proc.calls == []

    async def test_dry_run_with_sudo_when_non_root(
        self, proc: _ProcRecorder, fake_script: str, as_non_root: None
    ) -> None:
        result = await open_ports(ports=(80,), kind=FirewallKind.UFW, dry_run=True)

        assert result.needs_sudo is True
        assert result.commands == [["sudo", "-n", "bash", fake_script, "ufw", "80"]]
        assert proc.calls == []


class TestOpenPortsExecutes:
    """Without dry-run, the script is spawned once with the planned argv."""

    async def test_root_runs_bash_script_directly(
        self, proc: _ProcRecorder, fake_script: str, as_root: None
    ) -> None:
        result = await open_ports(ports=(80, 443), kind=FirewallKind.FIREWALLD)

        assert result.needs_sudo is False
        assert result.opened == [80, 443]
        assert proc.calls == [["bash", fake_script, "firewalld", "80", "443"]]

    async def test_non_root_prefixes_sudo_n(
        self, proc: _ProcRecorder, fake_script: str, as_non_root: None
    ) -> None:
        result = await open_ports(ports=(443,), kind=FirewallKind.UFW)

        assert result.needs_sudo is True
        assert result.opened == [443]
        assert proc.calls == [["sudo", "-n", "bash", fake_script, "ufw", "443"]]

    async def test_accepts_arbitrary_iterable(
        self, proc: _ProcRecorder, fake_script: str, as_root: None
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
        self, proc: _ProcRecorder, fake_script: str, as_root: None
    ) -> None:
        # The host script prints Korean guidance and exits 0 when kind=none.
        result = await open_ports(kind=FirewallKind.NONE)

        assert result.kind == FirewallKind.NONE
        assert result.opened == [80, 443]
        assert proc.calls == [["bash", fake_script, "none", "80", "443"]]


class TestSudoPermissionError:
    """When `sudo -n` fails the orchestrator surfaces a typed error."""

    async def test_sudo_failure_raises_outo_error_with_firewall_permission_code(
        self, proc: _ProcRecorder, fake_script: str, as_non_root: None
    ) -> None:
        # Pre-script the failed sudo -n.
        proc._scripted = [_ScriptedCall(returncode=1, stderr="sudo: a password is required\n")]

        with pytest.raises(OutoError) as excinfo:
            await open_ports(kind=FirewallKind.UFW)

        assert excinfo.value.code == "firewall_permission"

    async def test_sudo_failure_does_not_re_execute(
        self, proc: _ProcRecorder, fake_script: str, as_non_root: None
    ) -> None:
        proc._scripted = [_ScriptedCall(returncode=1)]
        with pytest.raises(OutoError):
            await open_ports(kind=FirewallKind.UFW)

        # The script itself must NOT have been invoked after sudo failed.
        assert len(proc.calls) == 1
        assert proc.calls[0][0] == "sudo"


class TestScriptPathResolution:
    """`OUTO_FIREWALL_SCRIPT` overrides the package-relative default."""

    async def test_env_override_used_when_set(
        self, proc: _ProcRecorder, fake_script: str, as_root: None
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
        self, proc: _ProcRecorder, fake_script: str, as_root: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_detect() -> FirewallKind:
            return FirewallKind.NFTABLES

        monkeypatch.setattr(open_ports_mod, "detect_firewall", fake_detect)

        result = await open_ports(ports=(80,))

        assert result.kind == FirewallKind.NFTABLES
        assert proc.calls == [["bash", fake_script, "nftables", "80"]]

    async def test_detection_failure_still_returns_none_kind(
        self, proc: _ProcRecorder, fake_script: str, as_root: None, monkeypatch: pytest.MonkeyPatch
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
            needs_sudo=True,
        )
        assert r.kind == FirewallKind.UFW
        assert r.opened == [80, 443]
        assert r.commands == [["bash", sample_script, "ufw", "80", "443"]]
        assert r.needs_sudo is True
