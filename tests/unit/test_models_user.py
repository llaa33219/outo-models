"""Round-trip tests for the `User` ORM model.

Covers create / read / update / delete, the unique constraints on `username`
and `email`, and the `is_active` property that gates API access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import Base, User, dispose_engines, get_engine, get_session_factory


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


class TestUserCreateRead:
    """`User` rows survive `commit()` and round-trip through a new session."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(
                    username="alice",
                    email="alice@example.com",
                    password_hash="$argon2id$v=19$m=65536,t=3,p=1$abc$xyz",
                )
            )
            await session.commit()

        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.username == "alice"))
            ).scalar_one()
            assert user.email == "alice@example.com"
            assert user.password_hash.startswith("$argon2id$")
            assert user.role == "user"
            assert user.status == "pending"
            assert user.display_name is None
            assert user.approved_at is None
            assert user.approved_by_id is None
            assert isinstance(user.created_at, datetime)
            assert isinstance(user.updated_at, datetime)

    async def test_explicit_status_and_role_persist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(
                    username="bob",
                    email="bob@example.com",
                    password_hash="hash",
                    role="admin",
                    status="approved",
                    display_name="Bob the Builder",
                )
            )
            await session.commit()

        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.username == "bob"))
            ).scalar_one()
            assert user.role == "admin"
            assert user.status == "approved"
            assert user.display_name == "Bob the Builder"


class TestUserUpdate:
    """Updates to mutable columns are visible after a fresh session."""

    async def test_update_status_and_approved_at(
        self, session_factory: async_sessionmaker[AsyncSession]
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
            carol_id = (
                await session.execute(
                    select(User.id).where(User.username == "carol")
                )
            ).scalar_one()

        approved_at = datetime.now(tz=UTC) + timedelta(minutes=1)
        async with session_factory() as session:
            user = await session.get(User, carol_id)
            assert user is not None
            user.status = "approved"
            user.approved_at = approved_at
            await session.commit()

        async with session_factory() as session:
            user = await session.get(User, carol_id)
            assert user is not None
            assert user.status == "approved"


class TestUserDelete:
    """Delete removes the row so a follow-up SELECT finds nothing."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            session.add(
                User(
                    username="dave",
                    email="dave@example.com",
                    password_hash="h",
                )
            )
            await session.commit()
            dave_id = (
                await session.execute(
                    select(User.id).where(User.username == "dave")
                )
            ).scalar_one()

        async with session_factory() as session:
            user = await session.get(User, dave_id)
            assert user is not None
            await session.delete(user)
            await session.commit()

        async with session_factory() as session:
            result = await session.execute(
                select(User).where(User.username == "dave")
            )
            assert result.scalar_one_or_none() is None


class TestUserUniqueConstraints:
    """Duplicate `username` and duplicate `email` are rejected at the DB."""

    async def test_duplicate_username_raises_integrity_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(username="erin", email="erin@example.com", password_hash="h")
            )
            await session.commit()

        async with session_factory() as session:
            session.add(
                User(username="erin", email="other@example.com", password_hash="h")
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_duplicate_email_raises_integrity_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(username="frank", email="frank@example.com", password_hash="h")
            )
            await session.commit()

        async with session_factory() as session:
            session.add(
                User(username="frank2", email="frank@example.com", password_hash="h")
            )
            with pytest.raises(IntegrityError):
                await session.commit()


class TestUserIsActive:
    """`is_active` mirrors `status == "approved"`; other statuses are inactive."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("approved", True),
            ("pending", False),
            ("denied", False),
            ("banned", False),
        ],
    )
    async def test_is_active_for_each_status(
        self, session_factory: async_sessionmaker[AsyncSession], status: str, expected: bool
    ) -> None:
        async with session_factory() as session:
            session.add(
                User(
                    username=f"user-{status}",
                    email=f"{status}@example.com",
                    password_hash="h",
                    status=status,
                )
            )
            await session.commit()

        async with session_factory() as session:
            user = (
                await session.execute(
                    select(User).where(User.username == f"user-{status}")
                )
            ).scalar_one()
            assert user.is_active is expected