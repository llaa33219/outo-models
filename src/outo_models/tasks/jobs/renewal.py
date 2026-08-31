"""Scheduled cert-renewal job body.

Wires the existing `tls.renewal.renewal_job` into the scheduler: build a
fresh `CaddyManager` per tick (so config reloads pick up on the next run),
delegate the check + nudge to `tls.renewal`, close the manager before
returning, and never let an exception escape — a transient blip must not
kill the scheduler loop.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from outo_models.config import Settings
from outo_models.tls.caddy_manager import CaddyManager
from outo_models.tls.renewal import CertHealth, renewal_job

_logger = structlog.get_logger("outo_models.tasks.jobs.renewal")


async def cert_renewal_job(
    settings: Settings,
    caddy_factory: Callable[[], CaddyManager],
) -> None:
    """Daily cert health check + caddy nudge for `settings.domain`.

    A fresh `CaddyManager` is constructed on every run so a config reload
    between ticks is honored automatically. The manager is closed in a
    `finally` so it is released even when the inner job raises — and any
    exception that escapes `tls.renewal.renewal_job` (it is documented as
    never-raising, but defense in depth) is logged and swallowed so the
    scheduler keeps ticking.
    """
    caddy = caddy_factory()
    try:
        health: CertHealth = await renewal_job(settings.domain, caddy)
    except Exception as exc:
        _logger.warning(
            "cert_renewal_job raised; swallowing to keep scheduler alive",
            domain=settings.domain,
            error=str(exc),
        )
        return
    finally:
        try:
            await caddy.close()
        except Exception as exc:
            _logger.warning(
                "caddy manager close failed during cert_renewal_job",
                domain=settings.domain,
                error=str(exc),
            )

    _logger.info(
        "cert_renewal_job completed",
        domain=settings.domain,
        ok=health.ok,
        days_remaining=health.days_remaining,
        error=health.error,
    )


__all__ = ["cert_renewal_job"]
