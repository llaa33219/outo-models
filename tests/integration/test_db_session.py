"""Integration tests for `outo_models.db.session`.

`session_scope()` is the contract every script / service uses, so it gets
explicit coverage for its two branches (commit on clean exit, rollback +
re-raise on exception), the cleanup happens on every path, and the
`get_session_factory` helper yields independent sessions per call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
    session_scope,
)


@pytest.fixture
async def factory(tmp_data_dir: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Build a per-test sqlite-backed engine + schema; dispose on teardown."""
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


class TestSessionScopeCommit:
    """`session_scope` commits on clean exit so the row is visible afterwards."""

    async def test_commit_on_clean_exit(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_scope() as session:
            session.add(
                User(
                    username="happy",
                    email="happy@example.com",
                    password_hash="h",
                )
            )

        async with session_scope() as verify_session:
            user = (
                await verify_session.execute(
                    select(User).where(User.username == "happy")
                )
            ).scalar_one()
            assert user.email == "happy@example.com"

    async def test_commit_persists_a_second_block(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_scope() as session:
            session.add(
                User(
                    username="second",
                    email="second@example.com",
                    password_hash="h",
                )
            )
        async with session_scope() as session:
            session.add(
                User(
                    username="third",
                    email="third@example.com",
                    password_hash="h",
                )
            )

        async with session_scope() as verify_session:
            users = (
                await verify_session.execute(
                    select(User).where(User.username.in_(("second", "third")))
                )
            ).scalars().all()
            assert {u.username for u in users} == {"second", "third"}


class TestSessionScopeRollback:
    """An exception inside `session_scope` rolls back and re-raises."""

    async def test_rollback_on_exception(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        class _BoomError(Exception):
            """Synthetic exception used to trigger rollback."""

        with pytest.raises(_BoomError, match="boom"):
            async with session_scope() as session:
                session.add(
                    User(
                        username="doomed",
                        email="doomed@example.com",
                        password_hash="h",
                    )
                )
                raise _BoomError("boom")

        async with session_scope() as verify_session:
            result = await verify_session.execute(
                select(User).where(User.username == "doomed")
            )
            assert result.scalar_one_or_none() is None


class TestSessionScopeCleanup:
    """`session_scope` re-enters cleanly after every exit path."""

    async def test_fresh_session_scope_works_after_clean_exit(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1

    async def test_fresh_session_scope_works_after_exception(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        sentinel = Exception("nope")
        with pytest.raises(Exception, match="nope"):
            async with session_scope() as session:
                raise sentinel
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1


class TestSessionFactoryReturnsSession:
    """`get_session_factory` yields a fresh session on every call."""

    async def test_each_session_is_independent(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session_a, factory() as session_b:
            assert session_a is not session_b
            result = await session_a.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
            result = await session_b.execute(text("SELECT 1"))
            assert result.scalar_one() == 1