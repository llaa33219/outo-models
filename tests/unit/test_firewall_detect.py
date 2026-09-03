"""Tests for `outo_models.firewall.detect`.

The detect module spawns subprocesses to probe for `firewalld`, `ufw`, and
`nftables`. These tests monkeypatch `asyncio.create_subprocess_exec` so they
NEVER touch the host's real firewall tools — every probe runs through a
`FakeProc` that returns whatever stdout/exit-code the test wants.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from outo_models.firewall import detect as detect_mod
from outo_models.firewall.detect import FirewallKind, detect_firewall, is_port_open


@dataclass
class _Call:
    """A single recorded `asyncio.create_subprocess_exec` invocation."""

    args: tuple[str, ...]
    # Optional override; when set, the fake proc ignores per-call `returncode`.
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _make_proc(call: _Call) -> _FakeProc:
    return _FakeProc(call.returncode, call.stdout, call.stderr)


class _FakeProc:
    """Minimal async-subprocess stub.

    Mirrors the `Process` shape we use: `communicate()` returns `(bytes, bytes)`.
    """

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()


class _Recorder:
    """Collects the call arguments to `asyncio.create_subprocess_exec`."""

    def __init__(self, scripted: list[_Call] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        # Pre-canned responses consumed in order; the recorder falls back to a
        # generic "not found" probe once the queue drains.
        self._scripted = list(scripted or [])

    def pop_next(self) -> _Call:
        if self._scripted:
            return self._scripted.pop(0)
        return _Call(args=(), returncode=127, stdout="", stderr="")

    async def __call__(self, *args: str, **kwargs: object) -> _FakeProc:
        # Strip the well-known kwargs so we record only argv; ignore unknown ones.
        self.calls.append(args)
        call = self.pop_next()
        # If a scripted response exists, use it; else synthesize "not found".
        return _make_proc(call)


@pytest.fixture
def proc(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    """Replace `asyncio.create_subprocess_exec` with a scripted fake."""
    recorder = _Recorder()
    monkeypatch.setattr(detect_mod.asyncio, "create_subprocess_exec", recorder)
    yield recorder


class TestFirewallKind:
    """`FirewallKind` is a closed enumeration the rest of the codebase switches on."""

    def test_values(self) -> None:
        assert {k.value for k in FirewallKind} == {"firewalld", "ufw", "nftables", "none"}

    def test_is_str_subclass(self) -> None:
        # StrEnum: usable as a plain string (e.g. argv) without conversion.
        assert FirewallKind.FIREWALLD == "firewalld"
        assert FirewallKind.UFW.value == "ufw"


class TestDetectFirewall:
    """`detect_firewall` probes firewalld → ufw → nftables → NONE in that order."""

    async def test_returns_firewalld_when_firewall_cmd_says_running(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=0, stdout="running\n")]
        assert await detect_firewall() == FirewallKind.FIREWALLD

    async def test_firewalld_probe_uses_state_subcommand(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=0, stdout="running\n")]
        await detect_firewall()
        assert proc.calls[0] == ("firewall-cmd", "--state")

    async def test_firewalld_says_not_running_falls_through_to_ufw(self, proc: _Recorder) -> None:
        # firewalld present but not running → exit 0 but stdout != "running"
        proc._scripted = [
            _Call((), returncode=0, stdout="not running\n"),
            _Call((), returncode=0, stdout="Status: active\n"),
        ]
        assert await detect_firewall() == FirewallKind.UFW

    async def test_returns_ufw_when_status_active(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call((), returncode=127),  # no firewall-cmd
            _Call((), returncode=0, stdout="Status: active\n"),
        ]
        assert await detect_firewall() == FirewallKind.UFW

    async def test_ufw_probe_uses_status_subcommand(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call((), returncode=127),
            _Call((), returncode=0, stdout="Status: active\n"),
        ]
        await detect_firewall()
        assert proc.calls[1] == ("ufw", "status")

    async def test_returns_nftables_when_nft_works(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call((), returncode=127),  # no firewall-cmd
            _Call((), returncode=127),  # no ufw
            _Call((), returncode=0, stdout="nftables v1.0.7\n"),
            _Call((), returncode=0, stdout="table inet filter {}\n"),
        ]
        assert await detect_firewall() == FirewallKind.NFTABLES

    async def test_nftables_requires_both_version_and_ruleset(self, proc: _Recorder) -> None:
        # `nft --version` works but `nft list ruleset` fails → not nftables.
        proc._scripted = [
            _Call((), returncode=127),
            _Call((), returncode=127),
            _Call((), returncode=0, stdout="nftables v1.0.7\n"),
            _Call((), returncode=1, stdout=""),
        ]
        assert await detect_firewall() == FirewallKind.NONE

    async def test_returns_none_when_no_binary_is_present(self, proc: _Recorder) -> None:
        # Every probe 127s (command not found).
        for _ in range(8):
            proc._scripted.append(_Call((), returncode=127))
        assert await detect_firewall() == FirewallKind.NONE

    async def test_short_circuits_at_first_match(self, proc: _Recorder) -> None:
        # firewalld matches; we must NOT probe ufw/nftables afterward.
        proc._scripted = [_Call((), returncode=0, stdout="running\n")]
        await detect_firewall()
        assert len(proc.calls) == 1


class TestIsPortOpen:
    """`is_port_open` returns True/False when determinable, None when not."""

    async def test_firewalld_query_returns_true(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=0, stdout="yes\n")]
        assert await is_port_open(443, FirewallKind.FIREWALLD) is True

    async def test_firewalld_query_returns_false(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=1, stdout="no\n")]
        assert await is_port_open(443, FirewallKind.FIREWALLD) is False

    async def test_firewalld_query_missing_binary_returns_none(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=127)]
        assert await is_port_open(443, FirewallKind.FIREWALLD) is None

    async def test_firewalld_query_uses_query_port_argv(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=0, stdout="yes\n")]
        await is_port_open(443, FirewallKind.FIREWALLD)
        assert proc.calls[0] == ("firewall-cmd", "--query-port=443/tcp")

    async def test_ufw_status_parses_allow(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call(
                (),
                returncode=0,
                stdout=(
                    "Status: active\n"
                    "To                         Action      From\n"
                    "443/tcp                    ALLOW IN    Anywhere\n"
                ),
            )
        ]
        assert await is_port_open(443, FirewallKind.UFW) is True

    async def test_ufw_status_parses_deny(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call(
                (),
                returncode=0,
                stdout=(
                    "Status: active\n"
                    "To                         Action      From\n"
                    "443/tcp                    DENY IN     Anywhere\n"
                ),
            )
        ]
        assert await is_port_open(443, FirewallKind.UFW) is False

    async def test_ufw_status_missing_port_returns_none(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call(
                (),
                returncode=0,
                stdout=(
                    "Status: active\n"
                    "To                         Action      From\n"
                    "22/tcp                     ALLOW IN    Anywhere\n"
                ),
            )
        ]
        assert await is_port_open(443, FirewallKind.UFW) is None

    async def test_ufw_status_inactive_returns_none(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=0, stdout="Status: inactive\n")]
        assert await is_port_open(443, FirewallKind.UFW) is None

    async def test_nftables_open_when_dport_in_ruleset(self, proc: _Recorder) -> None:
        proc._scripted = [
            _Call(
                (),
                returncode=0,
                stdout="table inet filter {\n  chain input {\n    tcp dport 443 accept\n  }\n}\n",
            )
        ]
        assert await is_port_open(443, FirewallKind.NFTABLES) is True

    async def test_nftables_closed_when_dport_absent(self, proc: _Recorder) -> None:
        ruleset = "table inet filter {\n  chain input {\n    tcp dport 22 accept\n  }\n}\n"
        proc._scripted = [_Call((), returncode=0, stdout=ruleset)]
        assert await is_port_open(443, FirewallKind.NFTABLES) is False

    async def test_nftables_unparseable_returns_none(self, proc: _Recorder) -> None:
        proc._scripted = [_Call((), returncode=1)]
        assert await is_port_open(443, FirewallKind.NFTABLES) is None

    async def test_none_kind_returns_none(self, proc: _Recorder) -> None:
        assert await is_port_open(443, FirewallKind.NONE) is None
        # No probe is issued when there is nothing to ask.
        assert proc.calls == []


def test_module_exports_match_contract() -> None:
    """The setup wizard (WP-14) imports these names — keep them stable."""
    assert hasattr(detect_mod, "FirewallKind")
    assert hasattr(detect_mod, "detect_firewall")
    assert hasattr(detect_mod, "is_port_open")
    # Sanity: asyncio.create_subprocess_exec stays importable on the module so
    # monkeypatching `asyncio.create_subprocess_exec` (per test) intercepts it.
    assert hasattr(detect_mod.asyncio, "create_subprocess_exec")


class TestMissingBinaries:
    """A missing firewall binary must mean 'not this backend', never a crash.

    Field failure: the container image ships no firewall tools, so
    `firewall-cmd` raised FileNotFoundError out of detect_firewall() and
    killed the setup wizard mid-run.
    """

    async def test_all_binaries_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _missing(*_a: object, **_k: object) -> object:
            raise FileNotFoundError("No such file or directory: 'firewall-cmd'")

        monkeypatch.setattr(detect_mod.asyncio, "create_subprocess_exec", _missing)
        assert await detect_mod.detect_firewall() is FirewallKind.NONE

    async def test_firewalld_missing_but_ufw_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _dispatch(*args: object, **_k: object) -> object:
            if args[0] == "firewall-cmd":
                raise FileNotFoundError("missing")
            return _FakeProc(0, "Status: active\n", "")

        monkeypatch.setattr(detect_mod.asyncio, "create_subprocess_exec", _dispatch)
        assert await detect_mod.detect_firewall() is FirewallKind.UFW
