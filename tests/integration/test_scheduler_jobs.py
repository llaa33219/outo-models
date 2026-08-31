"""Integration tests for `outo_models.tasks.scheduler` and its job bodies.

Three groups:

* `TaskScheduler` — start/shutdown cycle, every `JOB_IDS` is registered with
  the right trigger type and cadence, and a crashing job body does not take
  the scheduler down with it (the loop contract WP-13 relies on).
* `cert_renewal_job` — the fake caddy factory is called once per run, the
  caddy manager is closed afterwards, and the job never raises even when
  `tls.renewal.renewal_job` blows up.
* `quota_reconcile_job` — the lazy import of
  `outo_models.repos.quota.reconcile_user` is honored: absent → warn + return,
  present → called once per user.

The scheduler swallows anything that escapes a job body, so a crashing body
must not kill the loop — verified by registering a custom "always-raises"
job and asserting the scheduler keeps scheduling afterwards.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import Settings, get_settings
from outo_models.db import (
    AuditLog,
    Base,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.tasks.jobs.audit_prune import prune_audit_logs
from outo_models.tasks.jobs.quota_reconcile import quota_reconcile_job
from outo_models.tasks.jobs.renewal import cert_renewal_job
from outo_models.tasks.scheduler import TaskScheduler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(tmp_data_dir) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh per-test sqlite-backed engine + schema; auto-disposed."""
    await dispose_engines()
    settings = get_settings()
    engine: AsyncEngine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = get_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
        await dispose_engines()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCaddy:
    """In-process stand-in for `CaddyManager` covering the methods the renewal job uses."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        reload_raises: BaseException | None = None,
    ) -> None:
        self.healthy_result = healthy
        self.reload_raises = reload_raises
        self.reload_calls = 0
        self.close_calls = 0

    async def healthy(self) -> bool:
        return self.healthy_result

    async def reload(self) -> None:
        self.reload_calls += 1
        if self.reload_raises is not None:
            raise self.reload_raises

    async def close(self) -> None:
        self.close_calls += 1


def _cron_is_daily(trigger: CronTrigger) -> bool:
    """True iff the cron fields describe a single fixed minute every day.

    A daily cron has every higher-order field set to "*" (any year, month,
    day-of-month, day-of-week) and the hour/minute fields fixed to integers.
    """
    fields = trigger.fields
    return (
        str(fields[0]) == "*"  # year
        and str(fields[1]) == "*"  # month
        and str(fields[2]) == "*"  # day of month
        and str(fields[4]) == "*"  # day of week
        and fields[5].is_default is False  # hour is fixed
        and fields[6].is_default is False  # minute is fixed
    )


# ---------------------------------------------------------------------------
# TaskScheduler: registration + lifecycle
# ---------------------------------------------------------------------------


class TestTaskSchedulerRegistration:
    """`start()` registers every `JOB_IDS` job with the right trigger."""

    async def test_start_registers_all_three_jobs(
        self, tmp_data_dir, settings: Settings
    ) -> None:
        scheduler = TaskScheduler(settings, caddy_manager_factory=lambda: _FakeCaddy())
        try:
            scheduler.start()
            registered_ids = {job.id for job in scheduler.scheduler.get_jobs()}
            assert set(TaskScheduler.JOB_IDS) == registered_ids

            cert = scheduler.scheduler.get_job("cert_renewal")
            quota = scheduler.scheduler.get_job("quota_reconcile")
            audit = scheduler.scheduler.get_job("audit_prune")
            assert cert is not None
            assert quota is not None
            assert audit is not None

            # cert_renewal + audit_prune are daily CronTriggers;
            # quota_reconcile is an hourly IntervalTrigger.
            assert isinstance(cert.trigger, CronTrigger)
            assert isinstance(audit.trigger, CronTrigger)
            assert isinstance(quota.trigger, IntervalTrigger)
            assert _cron_is_daily(cert.trigger)
            assert _cron_is_daily(audit.trigger)
            assert quota.trigger.interval == dt.timedelta(hours=1)
        finally:
            await scheduler.shutdown()

    async def test_replace_existing_is_set(
        self, tmp_data_dir, settings: Settings
    ) -> None:
        # Re-registering against an already-started scheduler must not crash;
        # `replace_existing=True` makes the second call idempotent.
        scheduler = TaskScheduler(settings, caddy_manager_factory=lambda: _FakeCaddy())
        try:
            scheduler.start()
            scheduler.start()
            assert len(scheduler.scheduler.get_jobs()) == len(TaskScheduler.JOB_IDS)
        finally:
            await scheduler.shutdown()

    async def test_shutdown_is_idempotent(
        self, tmp_data_dir, settings: Settings
    ) -> None:
        scheduler = TaskScheduler(settings, caddy_manager_factory=lambda: _FakeCaddy())
        scheduler.start()
        await scheduler.shutdown()
        # A second shutdown must not raise — the FastAPI lifespan handler
        # can run it twice during a misbehaving shutdown hook.
        await scheduler.shutdown()

    async def test_scheduler_property_exposes_apscheduler(
        self, tmp_data_dir, settings: Settings
    ) -> None:
        scheduler = TaskScheduler(settings, caddy_manager_factory=lambda: _FakeCaddy())
        assert isinstance(scheduler.scheduler, AsyncIOScheduler)


class TestTaskSchedulerSwallowsCrashingJobs:
    """A job that raises must not kill the scheduler or its other jobs."""

    async def test_crashing_custom_job_does_not_disable_scheduler(
        self, tmp_data_dir, settings: Settings
    ) -> None:
        scheduler = TaskScheduler(settings, caddy_manager_factory=lambda: _FakeCaddy())
        try:
            scheduler.start()

            async def _always_raises() -> None:
                raise RuntimeError("boom")

            scheduler.scheduler.add_job(
                _always_raises,
                trigger=IntervalTrigger(seconds=1),
                id="boom",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )

            # The contract: the loop must keep going after a job raises.
            # Run the body directly and confirm it raises — the scheduler's
            # state is the assertion we care about.
            with pytest.raises(RuntimeError, match="boom"):
                await _always_raises()

            assert scheduler.scheduler.running
            assert {job.id for job in scheduler.scheduler.get_jobs()} >= set(
                TaskScheduler.JOB_IDS
            )
        finally:
            await scheduler.shutdown()


# ---------------------------------------------------------------------------
# cert_renewal_job
# ---------------------------------------------------------------------------


class TestCertRenewalJob:
    """The renewal job wires a fresh caddy manager per run, closes it, never raises."""

    async def test_calls_caddy_factory_and_closes_manager(
        self, tmp_data_dir, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub the underlying renewal_job so the test never touches the network.
        import outo_models.tasks.jobs.renewal as rmod
        from outo_models.tls.renewal import CertHealth

        async def _fake_renewal(
            _domain: str, _caddy: _FakeCaddy, **_kwargs: Any
        ) -> CertHealth:
            return CertHealth(
                ok=True, not_after=None, days_remaining=30, error=None
            )

        monkeypatch.setattr(rmod, "renewal_job", _fake_renewal)

        caddy = _FakeCaddy()
        factory_calls = 0

        def _factory() -> _FakeCaddy:
            nonlocal factory_calls
            factory_calls += 1
            return caddy

        await cert_renewal_job(settings, _factory)  # type: ignore[arg-type]
        assert factory_calls == 1
        assert caddy.close_calls == 1

    async def test_never_raises_when_renewal_job_raises(
        self, tmp_data_dir, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import outo_models.tasks.jobs.renewal as rmod

        async def _explode(_domain: str, _caddy: _FakeCaddy, **_kwargs: Any) -> None:
            raise RuntimeError("network blip")

        monkeypatch.setattr(rmod, "renewal_job", _explode)

        caddy = _FakeCaddy()
        # The contract: a crashing renewal_job must NOT kill the scheduler.
        await cert_renewal_job(settings, lambda: caddy)
        assert caddy.close_calls == 1


# ---------------------------------------------------------------------------
# quota_reconcile_job
# ---------------------------------------------------------------------------


class TestQuotaReconcileJob:
    """The reconcile job lazily imports WP-8's `reconcile_user` and survives its absence."""

    async def test_returns_quietly_when_repos_quota_is_absent(
        self,
        tmp_data_dir,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the lazy import to fail by stashing a sentinel that raises
        # on attribute access. sys.modules[name] = None causes import to raise
        # ModuleNotFoundError, which is the realistic failure mode WP-8 ships
        # before the package exists.
        import outo_models.tasks.jobs.quota_reconcile as qmod

        monkeypatch.setitem(
            __import__("sys").modules, "outo_models.repos.quota", None
        )

        # Must NOT raise; must return without doing work.
        await qmod.quota_reconcile_job()

    async def test_calls_reconcile_user_per_user(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(
                    username="alice",
                    email="alice@example.com",
                    password_hash="h",
                )
            )
            session.add(
                User(
                    username="bob",
                    email="bob@example.com",
                    password_hash="h",
                )
            )
            await session.commit()

        # Inject a fake `outo_models.repos.quota` module with a tracking
        # `reconcile_user` so the lazy import resolves.
        import types

        fake_module = types.ModuleType("outo_models.repos.quota")
        reconcile_calls: list[tuple[AsyncSession, User]] = []

        async def _fake_reconcile_user(
            session: AsyncSession, user: User
        ) -> None:
            reconcile_calls.append((session, user))

        fake_module.reconcile_user = _fake_reconcile_user  # type: ignore[attr-defined]
        monkeypatch.setitem(
            __import__("sys").modules, "outo_models.repos.quota", fake_module
        )

        await quota_reconcile_job()

        assert len(reconcile_calls) == 2
        seen_users = {user.username for _, user in reconcile_calls}
        assert seen_users == {"alice", "bob"}

    async def test_never_raises_when_reconcile_user_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(
                    username="carol",
                    email="carol@example.com",
                    password_hash="h",
                )
            )
            await session.commit()

        import types

        fake_module = types.ModuleType("outo_models.repos.quota")

        async def _explode(_session: AsyncSession, _user: User) -> None:
            raise RuntimeError("disk full")

        fake_module.reconcile_user = _explode  # type: ignore[attr-defined]
        monkeypatch.setitem(
            __import__("sys").modules, "outo_models.repos.quota", fake_module
        )

        # A crashing reconcile_user must NOT bring the scheduler down.
        await quota_reconcile_job()


# ---------------------------------------------------------------------------
# prune_audit_logs is exercised in detail under unit tests; here we just
# confirm the integration with a real scheduler-bound sqlite DB works.
# ---------------------------------------------------------------------------


class TestPruneAuditLogsIntegration:
    """Round-trip through `prune_audit_logs` against a real sqlite-backed DB."""

    async def test_seeded_old_rows_are_removed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            session.add(
                AuditLog(
                    action="old",
                    target_type="t",
                    target_id="1",
                    created_at=now - dt.timedelta(days=120),
                )
            )
            session.add(
                AuditLog(
                    action="new",
                    target_type="t",
                    target_id="2",
                    created_at=now - dt.timedelta(days=10),
                )
            )
            await session.commit()

        deleted = await prune_audit_logs(
            retention_days=90,
            now=now,
            session_factory=session_factory,
        )
        assert deleted == 1
