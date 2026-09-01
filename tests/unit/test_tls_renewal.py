"""Tests for `outo_models.tls.renewal`.

`check_cert_health` is exercised against a real local TLS server backed by
trustme — proving the stdlib SSL path actually works — plus the closed-port
and expired-cert error paths. `renewal_job` is exercised against an in-process
fake `CaddyManager` to verify the "reload only when unhealthy + caddy-healthy"
policy without spinning up Caddy.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import socket
import ssl
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import trustme
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from outo_models.exceptions import OutoError
from outo_models.tls.caddy_manager import CaddyManager, TlsConfig
from outo_models.tls.renewal import CertHealth, check_cert_health, renewal_job

# `tls/` resolves a closed port to `OSError` quickly; we cap the timeout to
# keep the test suite snappy even when the kernel hasn't yet RST'd.
_CLOSED_PORT_TIMEOUT = 2.0


def _free_port() -> int:
    """Ask the kernel for a port it knows is free, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _build_expired_cert(
    hostname: str = "localhost",
) -> tuple[bytes, bytes]:
    """Return `(cert_pem, key_pem)` for a self-signed cert that already expired.

    trustme has no knob for expiry, so for the expired-cert path we generate
    a cert directly with `cryptography`. `cryptography` is in the resolved
    dependency tree (trustme depends on it) and is therefore available.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=400))
        .not_valid_after(now - dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _redirect_open_connection(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """Rewrite `asyncio.open_connection` so `:443` calls land on `port`.

    The public API of `check_cert_health` pins to port 443, so tests port that
    # out to talk to the in-process TLS fixture.
    """
    original_open = asyncio.open_connection

    async def _redirect(*args: Any, **kwargs: Any) -> Any:
        if len(args) >= 2:
            new_args: tuple[Any, ...] = (args[0], port, *args[2:])
        else:
            new_args = args
        kwargs.pop("port", None)
        return await original_open(*new_args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", _redirect)


class _LocalTlsServer:
    """Async TLS server bound to a free local port.

    Two construction modes:
    - `from_trustme(leaf)`: configure an SSLContext with trustme's helper.
    - `from_pem(cert_pem, key_pem)`: load a manually-built cert (used to
      serve an expired cert, since trustme has no expiry knob).

    Spawns its own asyncio event loop on a dedicated daemon thread so the
    server's lifecycle is independent of the test event loop.
    """

    def __init__(self, ctx: ssl.SSLContext) -> None:
        self._ctx = ctx
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.AbstractServer | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._host: str | None = None
        self._port: int | None = None
        self._ready = threading.Event()

    @classmethod
    def from_trustme(cls, leaf: trustme.LeafCert) -> _LocalTlsServer:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        leaf.configure_cert(ctx)
        return cls(ctx)

    @classmethod
    def from_pem(cls, cert_pem: bytes, key_pem: bytes) -> _LocalTlsServer:
        import tempfile

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with (
            tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as cert_file,
            tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as key_file,
        ):
            cert_file.write(cert_pem)
            cert_file.flush()
            key_file.write(key_pem)
            key_file.flush()
            cert_path, key_path = cert_file.name, key_file.name
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return cls(ctx)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, host="127.0.0.1", port=0, ssl=self._ctx
        )
        socks = self._server.sockets
        assert socks is not None and socks
        self._host, self._port = socks[0].getsockname()[:2]
        self._ready.set()
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionResetError):
                await writer.wait_closed()

    @property
    def host(self) -> str:
        assert self._host is not None
        return self._host

    @property
    def port(self) -> int:
        assert self._port is not None
        return self._port

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=5.0), "TLS server did not become ready"

    def stop(self) -> None:
        if self._server is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._server.close)
        self._thread.join(timeout=5.0)
        with contextlib.suppress(RuntimeError):
            self._loop.close()


@pytest.fixture
def live_tls_server() -> Iterator[_LocalTlsServer]:
    """Yield a trustme-backed TLS server bound to a free local port."""
    ca = trustme.CA()
    leaf = ca.issue_cert("localhost")
    server = _LocalTlsServer.from_trustme(leaf)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def expired_tls_server() -> Iterator[_LocalTlsServer]:
    """Yield a TLS server presenting a cert that already expired 30 days ago."""
    cert_pem, key_pem = _build_expired_cert("localhost")
    server = _LocalTlsServer.from_pem(cert_pem, key_pem)
    server.start()
    try:
        yield server
    finally:
        server.stop()


class TestCheckCertHealthHappy:
    """A real handshake against a fresh trustme cert yields `ok=True`."""

    async def test_fresh_cert_yields_ok_with_days_remaining(
        self,
        live_tls_server: _LocalTlsServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _redirect_open_connection(monkeypatch, live_tls_server.port)
        health = await check_cert_health("localhost", timeout=5.0)
        assert health.ok is True
        assert health.days_remaining is not None
        assert health.days_remaining >= 0
        assert health.not_after is not None
        assert health.not_after > dt.datetime.now(dt.UTC)
        assert health.error is None


class TestCheckCertHealthExpired:
    """An expired cert is reported `ok=False` with negative `days_remaining`."""

    async def test_expired_cert_yields_not_ok(
        self,
        expired_tls_server: _LocalTlsServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _redirect_open_connection(monkeypatch, expired_tls_server.port)
        health = await check_cert_health("localhost", timeout=5.0)
        assert health.ok is False
        assert health.days_remaining is not None
        assert health.days_remaining < 0
        assert health.not_after is not None
        assert health.error is not None
        assert "expired" in health.error.lower()


class TestCheckCertHealthClosedPort:
    """A refused connection is reported `ok=False` — never raised."""

    async def test_closed_port_yields_error_not_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _free_port()  # confirm the port picker works

        async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(asyncio, "open_connection", _refuse)
        health = await check_cert_health("127.0.0.1", timeout=_CLOSED_PORT_TIMEOUT)
        assert health.ok is False
        assert health.not_after is None
        assert health.days_remaining is None
        assert health.error is not None
        assert "refused" in health.error.lower()


class TestRenewalJobPolicy:
    """`renewal_job` reloads only when cert is unhealthy AND Caddy is healthy."""

    class _FakeCaddy:
        """In-process stand-in for `CaddyManager` covering only the methods the job touches."""

        def __init__(
            self,
            *,
            healthy_result: bool = True,
            reload_raises: BaseException | None = None,
        ) -> None:
            # Field name `_healthy` (not `healthy`) so it doesn't shadow the
            # `healthy()` coroutine below.
            self._healthy = healthy_result
            self.reload_raises = reload_raises
            self.reload_calls = 0

        async def healthy(self) -> bool:
            return self._healthy

        async def reload(self) -> None:
            self.reload_calls += 1
            if self.reload_raises is not None:
                raise self.reload_raises

    @staticmethod
    async def _run_with_check_stub(
        caddy: _FakeCaddy,
        health: CertHealth,
    ) -> CertHealth:
        """Run `renewal_job` with `check_cert_health` stubbed to return `health`."""
        import outo_models.tls.renewal as rmod

        original = rmod.check_cert_health

        async def _stub(_domain: str, **_kwargs: Any) -> CertHealth:
            return health

        rmod.check_cert_health = _stub  # type: ignore[assignment]
        try:
            return await renewal_job("models.example.com", caddy)  # type: ignore[arg-type]
        finally:
            rmod.check_cert_health = original  # type: ignore[assignment]

    async def test_healthy_cert_does_not_trigger_reload(self) -> None:
        caddy = self._FakeCaddy()
        health = CertHealth(ok=True, not_after=None, days_remaining=30, error=None)
        result = await self._run_with_check_stub(caddy, health)
        assert result.ok is True
        assert caddy.reload_calls == 0

    async def test_unhealthy_cert_with_caddy_healthy_triggers_reload(self) -> None:
        caddy = self._FakeCaddy(healthy_result=True)
        health = CertHealth(ok=False, not_after=None, days_remaining=-5, error="expired")
        result = await self._run_with_check_stub(caddy, health)
        assert result.ok is False
        assert caddy.reload_calls == 1

    async def test_unhealthy_cert_with_caddy_unhealthy_does_not_reload(self) -> None:
        caddy = self._FakeCaddy(healthy_result=False)
        health = CertHealth(ok=False, not_after=None, days_remaining=-5, error="expired")
        result = await self._run_with_check_stub(caddy, health)
        assert result.ok is False
        assert caddy.reload_calls == 0

    async def test_caddy_reload_raises_outo_error_is_swallowed(self) -> None:
        caddy = self._FakeCaddy(
            healthy_result=True,
            reload_raises=OutoError("caddy down", code="caddy_unreachable"),
        )
        health = CertHealth(ok=False, not_after=None, days_remaining=-5, error="expired")
        # Must NOT raise — the scheduler cannot afford to die on a transient blip.
        result = await self._run_with_check_stub(caddy, health)
        assert result.ok is False
        assert caddy.reload_calls == 1


class TestRenewalJobAcceptsCaddyManager:
    """End-to-end check: `renewal_job` accepts a real `CaddyManager` instance."""

    async def test_signature_is_compatible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:

            async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise ConnectionRefusedError("refused")

            monkeypatch.setattr(asyncio, "open_connection", _refuse)
            # The job returns a CertHealth; never raises.
            result = await renewal_job("127.0.0.1", manager)
            assert result.ok is False
            assert result.error is not None
        finally:
            await manager.close()
