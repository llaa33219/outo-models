"""Background-task scheduler for outo-models.

`TaskScheduler` wraps `apscheduler.schedulers.asyncio.AsyncIOScheduler` and
owns the three periodic jobs that keep the server healthy:

* `cert_renewal` — daily TLS cert health check + caddy nudge.
* `quota_reconcile` — hourly per-user storage usage recompute.
* `audit_prune` — daily old `audit_logs` deletion.

Each job body is documented as never-raising so the scheduler loop stays
alive across transient blips; the scheduler does not register its own
exception listeners — the job bodies handle their own error reporting.

Persistence is intentionally in-memory (`MemoryJobStore`, APScheduler's
default). Surviving a process restart is left as future work; the
production deploy restarts the scheduler on every boot, so a missed tick
is recovered by the next run rather than replayed from disk.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

# APScheduler ships without type stubs; `import-untyped` is the standard
# mypy suppression for untyped third-party packages.
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.base import BaseTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from outo_models.config import Settings
from outo_models.tasks.jobs.audit_prune import prune_audit_logs
from outo_models.tasks.jobs.quota_reconcile import quota_reconcile_job
from outo_models.tasks.jobs.renewal import cert_renewal_job
from outo_models.tls.caddy_manager import CaddyManager

# Cadences — kept as named constants so tests can verify the same values
# the scheduler registers, without hardcoding cron strings in two places.
_CERT_RENEWAL_HOUR = 0  # daily at 00:00 UTC
_AUDIT_PRUNE_HOUR = 2  # daily at 02:00 UTC; offset from renewal so the
# two daily jobs don't race for the same connection pool.
_QUOTA_RECONCILE_INTERVAL_HOURS = 1  # hourly

# Mis-fire grace in seconds: a job that should have fired up to an hour ago
# is still worth running. Beyond that, APScheduler skips it.
_MISFIRE_GRACE_SECONDS = 3600


def _cert_renewal_trigger() -> BaseTrigger:
    """Daily CronTrigger at 00:00 UTC for the TLS cert check."""
    return CronTrigger(hour=_CERT_RENEWAL_HOUR, minute=0)


def _audit_prune_trigger() -> BaseTrigger:
    """Daily CronTrigger at 02:00 UTC for the audit-log prune."""
    return CronTrigger(hour=_AUDIT_PRUNE_HOUR, minute=0)


def _quota_reconcile_trigger() -> BaseTrigger:
    """Hourly IntervalTrigger for the per-user quota reconcile."""
    return IntervalTrigger(hours=_QUOTA_RECONCILE_INTERVAL_HOURS)


class TaskScheduler:
    """Periodic-job scheduler used by the FastAPI lifespan handler.

    The constructor wires three jobs into an in-memory `AsyncIOScheduler`;
    `start()` actually begins scheduling; `shutdown()` stops it cleanly.
    WP-13's server startup imports `TaskScheduler`, calls `start()` after
    the DB engine is ready, and calls `shutdown()` on lifespan exit.

    The `scheduler` property exposes the underlying `AsyncIOScheduler` so
    tests can assert on registration without re-implementing the queries.
    """

    JOB_IDS: ClassVar[tuple[str, ...]] = (
        "cert_renewal",
        "quota_reconcile",
        "audit_prune",
    )

    def __init__(
        self,
        settings: Settings,
        caddy_manager_factory: Callable[[], CaddyManager],
    ) -> None:
        self._settings = settings
        self._caddy_manager_factory = caddy_manager_factory
        self._scheduler = AsyncIOScheduler()
        self._started = False

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """Expose the underlying APScheduler instance for tests and diagnostics."""
        return self._scheduler

    def _register_jobs(self) -> None:
        """Add every `JOB_IDS` job with `replace_existing=True` + coalesce.

        The shared knobs (`max_instances=1`, `coalesce=True`,
        `misfire_grace_time=3600`) come from the WP-13 contract: at most
        one run at a time, late firings coalesce into a single run, and a
        job that missed its window by less than an hour is still executed.
        """
        common: dict[str, object] = {
            "replace_existing": True,
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": _MISFIRE_GRACE_SECONDS,
        }
        self._scheduler.add_job(
            cert_renewal_job,
            trigger=_cert_renewal_trigger(),
            id="cert_renewal",
            args=(self._settings, self._caddy_manager_factory),
            **common,
        )
        self._scheduler.add_job(
            quota_reconcile_job,
            trigger=_quota_reconcile_trigger(),
            id="quota_reconcile",
            **common,
        )
        self._scheduler.add_job(
            prune_audit_logs,
            trigger=_audit_prune_trigger(),
            id="audit_prune",
            **common,
        )

    def start(self) -> None:
        """Register every periodic job and start the scheduler loop.

        Calling `start()` twice is safe: `_register_jobs` uses
        `replace_existing=True`, so the second pass idempotently re-binds
        the same job ids without duplicating them.
        """
        self._register_jobs()
        if not self._started:
            self._scheduler.start()
            self._started = True

    async def shutdown(self, wait: bool = False) -> None:
        """Stop the scheduler; safe to call when it was never started.

        Args:
            wait: When True, block until all in-flight jobs finish. WP-13
                passes `wait=False` from its lifespan exit so the process
                can terminate promptly even if a job is mid-tick.
        """
        if self._started and self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            self._started = False


__all__ = ["TaskScheduler"]
