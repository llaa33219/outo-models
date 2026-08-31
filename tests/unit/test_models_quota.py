"""Round-trip tests for the `UserQuota` and `UserUsage` ORM models.

Covers create / read / update / delete and the unique `user_id` constraint on
both tables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)


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


async def _make_user(session: AsyncSession, username: str) -> int:
    session.add(User(username=username, email=f"{username}@example.com", password_hash="h"))
    await session.commit()
    return (
        await session.execute(select(User.id).where(User.username == username))
    ).scalar_one()


class TestUserQuotaRoundTrip:
    """`UserQuota.max_bytes` is settable on insert and updatable after the fact."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "alice")
            session.add(UserQuota(user_id=user_id, max_bytes=5 * 1024**3))
            await session.commit()

        async with session_factory() as session:
            quota = (
                await session.execute(
                    select(UserQuota).where(UserQuota.user_id == user_id)
                )
            ).scalar_one()
            assert quota.max_bytes == 5 * 1024**3

    async def test_update_max_bytes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "bob")
            session.add(UserQuota(user_id=user_id, max_bytes=1024**3))
            await session.commit()
            quota_id = (
                await session.execute(
                    select(UserQuota.id).where(UserQuota.user_id == user_id)
                )
            ).scalar_one()

        async with session_factory() as session:
            quota = await session.get(UserQuota, quota_id)
            assert quota is not None
            quota.max_bytes = 20 * 1024**3
            await session.commit()

        async with session_factory() as session:
            quota = await session.get(UserQuota, quota_id)
            assert quota is not None
            assert quota.max_bytes == 20 * 1024**3

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "carol")
            session.add(UserQuota(user_id=user_id, max_bytes=1024))
            await session.commit()
            quota_id = (
                await session.execute(
                    select(UserQuota.id).where(UserQuota.user_id == user_id)
                )
            ).scalar_one()

        async with session_factory() as session:
            quota = await session.get(UserQuota, quota_id)
            assert quota is not None
            await session.delete(quota)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(
                    select(UserQuota).where(UserQuota.user_id == user_id)
                )
            ).scalar_one_or_none() is None

    async def test_unique_user_id(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "dave")
            session.add(UserQuota(user_id=user_id, max_bytes=1024))
            await session.commit()

        async with session_factory() as session:
            session.add(UserQuota(user_id=user_id, max_bytes=2048))
            with pytest.raises(IntegrityError):
                await session.commit()


class TestUserUsageRoundTrip:
    """`UserUsage.used_bytes` defaults to 0 and is the quota-reconcile target."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "erin")
            session.add(UserUsage(user_id=user_id))
            await session.commit()

        async with session_factory() as session:
            usage = (
                await session.execute(
                    select(UserUsage).where(UserUsage.user_id == user_id)
                )
            ).scalar_one()
            assert usage.used_bytes == 0

    async def test_update_used_bytes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "frank")
            session.add(UserUsage(user_id=user_id, used_bytes=4096))
            await session.commit()
            usage_id = (
                await session.execute(
                    select(UserUsage.id).where(UserUsage.user_id == user_id)
                )
            ).scalar_one()

        async with session_factory() as session:
            usage = await session.get(UserUsage, usage_id)
            assert usage is not None
            usage.used_bytes = 8192
            await session.commit()

        async with session_factory() as session:
            usage = await session.get(UserUsage, usage_id)
            assert usage is not None
            assert usage.used_bytes == 8192

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "greg")
            session.add(UserUsage(user_id=user_id, used_bytes=1024))
            await session.commit()
            usage_id = (
                await session.execute(
                    select(UserUsage.id).where(UserUsage.user_id == user_id)
                )
            ).scalar_one()

        async with session_factory() as session:
            usage = await session.get(UserUsage, usage_id)
            assert usage is not None
            await session.delete(usage)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(
                    select(UserUsage).where(UserUsage.user_id == user_id)
                )
            ).scalar_one_or_none() is None

    async def test_unique_user_id(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "harry")
            session.add(UserUsage(user_id=user_id, used_bytes=0))
            await session.commit()

        async with session_factory() as session:
            session.add(UserUsage(user_id=user_id, used_bytes=128))
            with pytest.raises(IntegrityError):
                await session.commit()