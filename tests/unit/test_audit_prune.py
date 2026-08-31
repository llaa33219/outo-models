"""Unit tests for `outo_models.tasks.jobs.audit_prune.prune_audit_logs`.

Exercises the delete-older-than-retention contract end to end against a real
sqlite-backed `AuditLog` table: seeds old + new rows, asserts only the old
ones disappear, the deleted count is accurate, and the injectable `now`
shifts the cutoff so a future / past `now` selects the right slice.

The session_scope / now hooks exist for exactly this reason: keeping the
real SQLAlchemy delete path under test without freezing wall-clock time.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    AuditLog,
    Base,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.tasks.jobs.audit_prune import prune_audit_logs


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


async def _seed(
    session: AsyncSession,
    *,
    now: dt.datetime,
    ages_days: tuple[int, ...],
    actions: tuple[str, ...],
) -> None:
    """Insert one AuditLog per `(age_days, action)` with explicit `created_at`.

    Bypasses the model default so the test owns the timestamps directly.
    """
    for age, action in zip(ages_days, actions, strict=True):
        created = now - dt.timedelta(days=age)
        session.add(
            AuditLog(
                action=action,
                target_type="test",
                target_id=str(age),
                created_at=created,
            )
        )
    await session.commit()


async def _remaining_actions(session: AsyncSession) -> set[str]:
    """Return the set of `action` strings for every AuditLog still in the table."""
    rows = (await session.execute(select(AuditLog.action))).scalars().all()
    return set(rows)


class TestPruneAuditLogsHappy:
    """Rows older than the retention window are deleted; newer rows survive."""

    async def test_only_old_rows_are_deleted(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fixed_now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            await _seed(
                session,
                now=fixed_now,
                ages_days=(120, 91, 90, 30, 0),
                actions=(
                    "old.120d",
                    "old.91d",
                    "boundary.90d",
                    "new.30d",
                    "new.0d",
                ),
            )

        deleted = await prune_audit_logs(
            retention_days=90,
            now=fixed_now,
            session_factory=session_factory,
        )

        assert deleted == 2
        async with session_factory() as session:
            assert await _remaining_actions(session) == {
                "boundary.90d",
                "new.30d",
                "new.0d",
            }

    async def test_returns_zero_when_nothing_to_prune(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fixed_now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            await _seed(
                session,
                now=fixed_now,
                ages_days=(1, 10),
                actions=("fresh.1d", "fresh.10d"),
            )

        deleted = await prune_audit_logs(
            retention_days=90,
            now=fixed_now,
            session_factory=session_factory,
        )

        assert deleted == 0
        async with session_factory() as session:
            assert await _remaining_actions(session) == {"fresh.1d", "fresh.10d"}

    async def test_returns_count_for_fully_stale_table(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fixed_now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            await _seed(
                session,
                now=fixed_now,
                ages_days=(100, 200, 365),
                actions=("stale.a", "stale.b", "stale.c"),
            )

        deleted = await prune_audit_logs(
            retention_days=90,
            now=fixed_now,
            session_factory=session_factory,
        )

        assert deleted == 3
        async with session_factory() as session:
            assert await _remaining_actions(session) == set()


class TestPruneAuditLogsInjectedNow:
    """An injected `now` shifts the cutoff so the test owns the wall clock."""

    async def test_past_now_keeps_row_that_present_now_would_prune(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # One row at age 91d. With `now=reference_now` the cutoff is reference_now-90d,
        # so the row is pruned. Pulling `now` 30 days into the past moves the cutoff
        # to reference_now-120d, which is AFTER the row, so the row survives.
        reference_now = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            await _seed(
                session,
                now=reference_now,
                ages_days=(91,),
                actions=("ninetyone",),
            )

        past_now = reference_now - dt.timedelta(days=30)
        deleted = await prune_audit_logs(
            retention_days=90,
            now=past_now,
            session_factory=session_factory,
        )
        assert deleted == 0
        async with session_factory() as session:
            assert await _remaining_actions(session) == {"ninetyone"}

    async def test_future_now_prunes_row_that_present_now_would_keep(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # One row at age 60d. With `now=reference_now` the cutoff is reference_now-90d,
        # so the row survives. Pushing `now` 60 days into the future moves the cutoff
        # to reference_now-30d, so the row is pruned.
        reference_now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            await _seed(
                session,
                now=reference_now,
                ages_days=(60,),
                actions=("sixty",),
            )

        future_now = reference_now + dt.timedelta(days=60)
        deleted = await prune_audit_logs(
            retention_days=90,
            now=future_now,
            session_factory=session_factory,
        )
        assert deleted == 1
        async with session_factory() as session:
            assert await _remaining_actions(session) == set()


class TestPruneAuditLogsSessionFactoryOverride:
    """A custom `session_factory` is used instead of the default `session_scope`."""

    async def test_uses_injected_session_factory(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fixed_now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
        async with session_factory() as session:
            await _seed(
                session,
                now=fixed_now,
                ages_days=(120, 5),
                actions=("stale", "fresh"),
            )

        @asynccontextmanager
        async def _override() -> AbstractAsyncContextManager[Any]:
            # Reuse the test's factory so we still observe the same DB, but
            # prove the prune code went through this hook.
            async with session_factory() as session:
                yield session

        deleted = await prune_audit_logs(
            retention_days=90,
            now=fixed_now,
            session_factory=_override,
        )

        assert deleted == 1
        async with session_factory() as session:
            assert await _remaining_actions(session) == {"fresh"}
