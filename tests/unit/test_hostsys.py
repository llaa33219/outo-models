"""Unit tests for outo_models.hostsys (rootless low-port sysctl handling)."""

from __future__ import annotations

from pathlib import Path

import pytest

from outo_models.exceptions import OutoError
from outo_models.hostsys import (
    HOST_LOW_PORTS_COMMAND,
    ensure_low_ports,
    low_ports_blocked,
    unprivileged_port_start,
)


@pytest.fixture
def sysctl_file(tmp_path: Path) -> Path:
    path = tmp_path / "ip_unprivileged_port_start"
    path.write_text("1024\n", encoding="ascii")
    return path


class TestUnprivilegedPortStart:
    def test_reads_value(self, sysctl_file: Path) -> None:
        assert unprivileged_port_start(sysctl_file) == 1024

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert unprivileged_port_start(tmp_path / "absent") is None

    def test_garbage_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "bad"
        path.write_text("not-a-number\n", encoding="ascii")
        assert unprivileged_port_start(path) is None


class TestLowPortsBlocked:
    def test_blocked_when_threshold_above_port(self, sysctl_file: Path) -> None:
        assert low_ports_blocked(80, sysctl_path=sysctl_file) is True

    def test_not_blocked_when_threshold_at_port(self, sysctl_file: Path) -> None:
        sysctl_file.write_text("80\n", encoding="ascii")
        assert low_ports_blocked(80, sysctl_path=sysctl_file) is False

    def test_unreadable_threshold_means_not_blocked(self, tmp_path: Path) -> None:
        assert low_ports_blocked(80, sysctl_path=tmp_path / "absent") is False


class TestEnsureLowPorts:
    async def test_noop_when_not_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "outo_models.hostsys.low_ports_blocked",
            lambda _min, *, sysctl_path=None: False,
        )
        result = await ensure_low_ports(80)
        assert result.was_blocked is False
        assert result.commands == []

    async def test_in_container_raises_with_host_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("outo_models.hostsys.low_ports_blocked", lambda *a, **k: True)
        monkeypatch.setattr("outo_models.hostsys.in_container", lambda: True)
        with pytest.raises(OutoError) as excinfo:
            await ensure_low_ports(80)
        assert excinfo.value.code == "low_ports_host_required"
        assert HOST_LOW_PORTS_COMMAND in str(excinfo.value)

    async def test_dry_run_plans_without_spawning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("outo_models.hostsys.low_ports_blocked", lambda *a, **k: True)
        monkeypatch.setattr("outo_models.hostsys.in_container", lambda: False)
        monkeypatch.setenv("OUTO_LOW_PORTS_SCRIPT", "/x/enable-low-ports.sh")
        result = await ensure_low_ports(80, dry_run=True)
        assert result.was_blocked is True
        assert result.commands == [["bash", "/x/enable-low-ports.sh", "80"]]

    async def test_script_failure_maps_to_typed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            returncode = 1

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"sysctl: permission denied"

        async def _spawn(*_a: object, **_k: object) -> _Proc:
            return _Proc()

        monkeypatch.setattr("outo_models.hostsys.low_ports_blocked", lambda *a, **k: True)
        monkeypatch.setattr("outo_models.hostsys.in_container", lambda: False)
        monkeypatch.setattr("outo_models.hostsys.asyncio.create_subprocess_exec", _spawn)
        with pytest.raises(OutoError) as excinfo:
            await ensure_low_ports(80)
        assert excinfo.value.code == "low_ports_command_failed"
        assert "sysctl: permission denied" in str(excinfo.value)
