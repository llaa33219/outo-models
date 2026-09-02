"""Tests for `outo_models.utils.net`.

`is_ip_address` is a pure classifier — the parametrized matrix covers every
input class the wizard / settings / middleware actually feed it.

`detect_lan_ipv4` exercises the UDP `connect()` trick against an injected
fake socket so it never opens a real socket in the test runner.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from outo_models.utils.net import detect_lan_ipv4, is_ip_address


class _FakeSocket:
    """Records the connect() target and returns a fixed local addr."""

    def __init__(self, family: int, kind: int, local_addr: tuple[str, int]) -> None:
        self._family = family
        self._kind = kind
        self._local_addr = local_addr
        self.connect_target: tuple[str, int] | None = None
        self.closed = False

    def connect(self, target: tuple[str, int]) -> None:
        self.connect_target = target

    def getsockname(self) -> tuple[str, int]:
        return self._local_addr

    def close(self) -> None:
        self.closed = True


class _RaisingSocket:
    def __init__(self, family: int, kind: int) -> None:
        self._family = family
        self._kind = kind

    def connect(self, _target: tuple[str, int]) -> None:
        raise OSError("network unreachable")

    def getsockname(self) -> tuple[str, int]:
        return ("", 0)

    def close(self) -> None:
        return None


class _EmptyAddrSocket:
    def __init__(self, family: int, kind: int) -> None:
        self._family = family
        self._kind = kind

    def connect(self, _target: tuple[str, int]) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        return ("", 0)

    def close(self) -> None:
        return None


class _MalformedAddrSocket:
    def __init__(self, family: int, kind: int) -> None:
        self._family = family
        self._kind = kind

    def connect(self, _target: tuple[str, int]) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        # An address that won't round-trip through `ipaddress.ip_address`
        # — the function must still return None, not raise.
        return ("definitely-not-an-ip", 0)

    def close(self) -> None:
        return None


class TestIsIpAddress:
    """Pure classifier — every input class is parametrized."""

    @pytest.mark.parametrize(
        "value",
        [
            "127.0.0.1",
            "0.0.0.0",
            "255.255.255.255",
            "192.168.1.1",
            "10.0.0.1",
            "203.0.113.42",
        ],
    )
    def test_ipv4_addresses_are_accepted(self, value: str) -> None:
        assert is_ip_address(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "::1",
            "::",
            "2001:db8::1",
            "fe80::1",
            "fd00::1",
        ],
    )
    def test_ipv6_addresses_are_accepted(self, value: str) -> None:
        assert is_ip_address(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "models.example.com",
            "example.com",
            "localhost",
            "not_an_ip",
            "192.168.1",
            "192.168.1.1.1",
            "256.0.0.1",
            "999.999.999.999",
            "192.168.1.1/24",
            "192.168.1.1:80",
            "1.2.3",
            "::g",
            "2001:db8::1::",
        ],
    )
    def test_non_addresses_are_rejected(self, value: str) -> None:
        assert is_ip_address(value) is False

    def test_strips_whitespace_before_classifying(self) -> None:
        assert is_ip_address("  192.168.1.1  ") is True
        assert is_ip_address("  models.example.com  ") is False

    def test_empty_after_strip_is_rejected(self) -> None:
        assert is_ip_address("   ") is False


class TestDetectLanIpv4:
    """`detect_lan_ipv4` uses the UDP `connect()` trick; the test injects a
    fake socket so it never touches the real network stack."""

    def test_returns_kernel_local_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeSocket(socket.AF_INET, socket.SOCK_DGRAM, ("192.168.1.42", 0))

        def _factory(*_args: Any, **_kwargs: Any) -> _FakeSocket:
            return fake

        monkeypatch.setattr(socket, "socket", _factory)
        result = detect_lan_ipv4()
        assert result == "192.168.1.42"
        # The probe is documented as TEST-NET-1:80 — verify it stayed on
        # the book. Any change here breaks the no-traffic guarantee.
        assert fake.connect_target == ("192.0.2.1", 80)
        assert fake.closed is True

    def test_returns_none_on_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _factory(*_args: Any, **_kwargs: Any) -> _RaisingSocket:
            return _RaisingSocket(socket.AF_INET, socket.SOCK_DGRAM)

        monkeypatch.setattr(socket, "socket", _factory)
        assert detect_lan_ipv4() is None

    def test_returns_none_when_getsockname_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _factory(*_args: Any, **_kwargs: Any) -> _EmptyAddrSocket:
            return _EmptyAddrSocket(socket.AF_INET, socket.SOCK_DGRAM)

        monkeypatch.setattr(socket, "socket", _factory)
        assert detect_lan_ipv4() is None

    def test_returns_none_when_getsockname_malformed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _factory(*_args: Any, **_kwargs: Any) -> _MalformedAddrSocket:
            return _MalformedAddrSocket(socket.AF_INET, socket.SOCK_DGRAM)

        monkeypatch.setattr(socket, "socket", _factory)
        assert detect_lan_ipv4() is None

    def test_uses_udp_dgram(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole "no traffic" trick depends on SOCK_DGRAM; assert the
        # call shape explicitly so a refactor can't silently switch to TCP.
        captured: dict[str, Any] = {}

        def _factory(family: int, kind: int, *_args: Any, **_kwargs: Any) -> Any:
            captured["family"] = family
            captured["kind"] = kind
            return _FakeSocket(family, kind, ("10.0.0.5", 0))

        monkeypatch.setattr(socket, "socket", _factory)
        detect_lan_ipv4()
        assert captured["family"] == socket.AF_INET
        assert captured["kind"] == socket.SOCK_DGRAM


__all__: list[str] = []
