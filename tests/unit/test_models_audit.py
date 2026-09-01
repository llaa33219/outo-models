"""Round-trip tests for the `AuditLog` ORM model.

Covers create / read / update / delete, the nullable `actor_id`, and the
indexed `action` / `created_at` columns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import AuditLog, Base, dispose_engines, get_engine, get_session_factory


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


class TestAuditLogCreateRead:
    """Audit rows can be inserted with or without an actor and round-trip."""

    async def test_system_entry_without_actor(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(
                AuditLog(
                    action="system.cleanup",
                    target_type="audit_log",
                    target_id="0",
                    detail='{"deleted":42}',
                    ip="127.0.0.1",
                )
            )
            await session.commit()

        async with session_factory() as session:
            entry = (
                await session.execute(select(AuditLog).where(AuditLog.action == "system.cleanup"))
            ).scalar_one()
            assert entry.actor_id is None
            assert entry.target_type == "audit_log"
            assert entry.target_id == "0"
            assert entry.detail == '{"deleted":42}'
            assert entry.ip == "127.0.0.1"

    async def test_user_entry_with_actor(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(
                AuditLog(
                    actor_id=42,
                    action="user.signup",
                    target_type="user",
                    target_id="42",
                )
            )
            await session.commit()

        async with session_factory() as session:
            entry = (
                await session.execute(select(AuditLog).where(AuditLog.action == "user.signup"))
            ).scalar_one()
            assert entry.actor_id == 42
            assert entry.detail is None
            assert entry.ip is None


class TestAuditLogUpdate:
    """Updates to `detail` are visible after a fresh session."""

    async def test_update_detail(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            session.add(AuditLog(action="repo.push", target_type="repo", target_id="1"))
            await session.commit()
            entry_id = (
                await session.execute(select(AuditLog.id).where(AuditLog.action == "repo.push"))
            ).scalar_one()

        async with session_factory() as session:
            entry = await session.get(AuditLog, entry_id)
            assert entry is not None
            entry.detail = '{"sha":"deadbeef"}'
            await session.commit()

        async with session_factory() as session:
            entry = await session.get(AuditLog, entry_id)
            assert entry is not None
            assert entry.detail == '{"sha":"deadbeef"}'


class TestAuditLogDelete:
    """Delete removes the row."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            session.add(AuditLog(action="repo.delete", target_type="repo", target_id="7"))
            await session.commit()
            entry_id = (
                await session.execute(select(AuditLog.id).where(AuditLog.action == "repo.delete"))
            ).scalar_one()

        async with session_factory() as session:
            entry = await session.get(AuditLog, entry_id)
            assert entry is not None
            await session.delete(entry)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(select(AuditLog).where(AuditLog.action == "repo.delete"))
            ).scalar_one_or_none() is None
