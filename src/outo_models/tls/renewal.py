"""TLS certificate health check + ACME renewal nudging.

Two functions:

* `check_cert_health(domain)` performs a real TLS handshake to `domain:443`,
  inspects the peer certificate's `notAfter`, and reports `ok`/days-remaining.
  Errors are caught and surfaced as a `CertHealth(ok=False, error=...)` —
  this function is the hot path of the daily scheduler, and an exception here
  would silently kill the renewal loop.

* `renewal_job(domain, caddy)` is the scheduled-job body that ties everything
  together. It calls `check_cert_health`; if the cert is unhealthy AND Caddy's
  admin API is healthy, it nudges Caddy to re-issue (which in turn re-runs
  ACME if needed). It never raises — every error mode is logged at warning
  level and reflected in the returned `CertHealth`.

Why this lives separately from `caddy_manager.py`: the scheduler wiring
(APScheduler job registration) is WP-11's work, but the health-check / nudge
logic is a property of the TLS layer and is what WP-11 will call.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ssl
from dataclasses import dataclass

import structlog
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from outo_models.exceptions import OutoError
from outo_models.tls.caddy_manager import CaddyManager

# Bound the entire handshake so a stalled endpoint cannot hang the scheduler.
_DEFAULT_TIMEOUT: float = 10.0

# Module logger — a single name keeps structlog's filtering consistent and
# lets the production JSON pipeline grep `outo_models.tls.renewal` cleanly.
_logger = structlog.get_logger("outo_models.tls.renewal")


@dataclass(frozen=True, slots=True)
class CertHealth:
    """Snapshot of one cert's freshness.

    Attributes:
        ok: True iff a cert was retrieved and `days_remaining >= 0`.
        not_after: The cert's `notAfter` field as a tz-aware UTC datetime, or
            `None` when the handshake / parse failed.
        days_remaining: Integer days until `not_after`, or `None` on failure.
            Negative when the cert has already expired.
        error: Human-readable failure message, or `None` on success.
    """

    ok: bool
    not_after: dt.datetime | None
    days_remaining: int | None
    error: str | None


def _build_client_context() -> ssl.SSLContext:
    """SSL context for the *client* side of the handshake.

    We deliberately disable hostname verification and certificate trust
    verification. `check_cert_health` is a *freshness* check, not a *trust*
    check — the point is to inspect the cert the server actually presents,
    regardless of validity. Trust is Caddy's concern (the operator can curl
    the public endpoint with `httpx` and the OS trust store); this function
    only needs to read the cert to compute `days_remaining`.

    With `verify_mode=CERT_NONE`, OpenSSL still negotiates TLS and exposes
    the peer certificate in DER form via `sslobject.getpeercert(binary_form=True)`,
    which we parse with `cryptography` below.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_der_cert(der: bytes) -> dt.datetime:
    """Parse a DER-encoded X.509 cert and return its `not_after` (tz-aware UTC)."""
    cert = x509.load_der_x509_certificate(der, default_backend())
    # `not_valid_after_utc` is tz-aware in cryptography>=42.
    return cert.not_valid_after_utc


async def check_cert_health(
    domain: str,
    timeout: float = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — caller-driven timeout
) -> CertHealth:
    """TLS-handshake `domain:443`, return the peer cert's freshness.

    Never raises. Every error mode — connect refused, handshake failed,
    malformed cert, expired cert — is reported as a `CertHealth` with
    `ok=False` and a meaningful `error` string.
    """
    ctx = _build_client_context()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(domain, 443, ssl=ctx, server_hostname=domain),
            timeout=timeout,
        )
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        return CertHealth(
            ok=False,
            not_after=None,
            days_remaining=None,
            error=f"connection to {domain}:443 failed: {exc}",
        )

    try:
        # The StreamWriter's transport carries an `ssl_object` extra_info that
        # exposes the underlying `ssl.SSLObject`. We only read its public API.
        ssl_object = writer.transport.get_extra_info("ssl_object")
        if ssl_object is None:
            return CertHealth(
                ok=False,
                not_after=None,
                days_remaining=None,
                error="no ssl object on transport — handshake may not have completed",
            )
        der_bytes = ssl_object.getpeercert(binary_form=True)
        if not der_bytes:
            return CertHealth(
                ok=False,
                not_after=None,
                days_remaining=None,
                error="server presented no certificate",
            )
        not_after = _parse_der_cert(der_bytes)
    except (OSError, ssl.SSLError, ValueError, TypeError) as exc:
        return CertHealth(
            ok=False,
            not_after=None,
            days_remaining=None,
            error=f"failed to parse peer certificate: {exc}",
        )
    finally:
        writer.close()
        with contextlib.suppress(OSError, ConnectionResetError):
            await writer.wait_closed()

    now = dt.datetime.now(dt.UTC)
    days_remaining = (not_after - now).days
    if days_remaining < 0:
        return CertHealth(
            ok=False,
            not_after=not_after,
            days_remaining=days_remaining,
            error=f"certificate expired {abs(days_remaining)} days ago",
        )
    return CertHealth(
        ok=True,
        not_after=not_after,
        days_remaining=days_remaining,
        error=None,
    )


async def renewal_job(domain: str, caddy: CaddyManager) -> CertHealth:
    """Scheduled body: check the cert, nudge Caddy if unhealthy + Caddy-healthy.

    The scheduler invokes this daily. The function never raises — a transient
    network blip or a Caddy restart that happens to coincide with our reload
    must not bring the scheduler down.
    """
    try:
        health = await check_cert_health(domain)
    except (OSError, ssl.SSLError) as exc:
        # Belt-and-braces: `check_cert_health` is designed not to raise, but
        # if a future change slips through, the scheduler still survives.
        return CertHealth(
            ok=False,
            not_after=None,
            days_remaining=None,
            error=f"unexpected error during cert health check: {exc}",
        )

    if health.ok:
        return health

    # Unhealthy — only nudge when the Caddy admin API is reachable. Otherwise
    # the nudge itself would fail noisily and we'd waste cycles.
    try:
        caddy_ok = await caddy.healthy()
    except Exception as exc:
        caddy_ok = False
        _logger.warning(
            "caddy health probe failed during renewal check",
            domain=domain,
            error=str(exc),
        )

    if not caddy_ok:
        _logger.warning(
            "certificate unhealthy but caddy admin unreachable; skipping nudge",
            domain=domain,
            error=health.error,
        )
        return health

    _logger.warning(
        "certificate unhealthy; nudging caddy reload to trigger ACME renewal",
        domain=domain,
        error=health.error,
        days_remaining=health.days_remaining,
    )
    try:
        await caddy.reload()
    except OutoError as exc:
        # caddy_unreachable, dns_upstream, etc. — log + swallow.
        _logger.warning(
            "caddy reload nudge failed",
            domain=domain,
            code=exc.code,
            error=str(exc),
        )
    except Exception as exc:
        _logger.warning(
            "caddy reload nudge raised unexpected error",
            domain=domain,
            error=str(exc),
        )

    return health


__all__: list[str] = ["CertHealth", "check_cert_health", "renewal_job"]
