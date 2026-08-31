"""Round-trip tests for the `Approval` ORM model.

Covers create / read / update / delete and the unique constraint on `user_id`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Approval,
    Base,
    User,
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


class TestApprovalCreateRead:
    """Approval rows round-trip with their FK to `users`."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "alice")
            session.add(Approval(user_id=user_id))
            await session.commit()

        async with session_factory() as session:
            approval = (
                await session.execute(
                    select(Approval).where(Approval.user_id == user_id)
                )
            ).scalar_one()
            assert approval.decision == "pending"
            assert approval.decided_by_id is None
            assert approval.reason is None
            assert approval.decided_at is None

    async def test_create_with_decision_and_reason(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "bob")
            admin_id = await _make_user(session, "admin1")
            decided_at = datetime.now(tz=UTC)
            session.add(
                Approval(
                    user_id=user_id,
                    decided_by_id=admin_id,
                    decision="approved",
                    reason="looks good",
                    decided_at=decided_at,
                )
            )
            await session.commit()

        async with session_factory() as session:
            approval = (
                await session.execute(
                    select(Approval).where(Approval.user_id == user_id)
                )
            ).scalar_one()
            assert approval.decided_by_id == admin_id
            assert approval.decision == "approved"
            assert approval.reason == "looks good"


class TestApprovalUpdate:
    """A pending approval can be decided in place."""

    async def test_decide_pending_approval(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "carol")
            session.add(Approval(user_id=user_id))
            await session.commit()
            approval_id = (
                await session.execute(
                    select(Approval.id).where(Approval.user_id == user_id)
                )
            ).scalar_one()

        async with session_factory() as session:
            admin_id = await _make_user(session, "ops")
            approval = await session.get(Approval, approval_id)
            assert approval is not None
            approval.decision = "denied"
            approval.decided_by_id = admin_id
            approval.reason = "no thanks"
            approval.decided_at = datetime.now(tz=UTC)
            await session.commit()

        async with session_factory() as session:
            approval = await session.get(Approval, approval_id)
            assert approval is not None
            assert approval.decision == "denied"
            assert approval.decided_by_id == admin_id


class TestApprovalDelete:
    """Delete removes the row."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "dave")
            session.add(Approval(user_id=user_id))
            await session.commit()
            approval_id = (
                await session.execute(
                    select(Approval.id).where(Approval.user_id == user_id)
                )
            ).scalar_one()

        async with session_factory() as session:
            approval = await session.get(Approval, approval_id)
            assert approval is not None
            await session.delete(approval)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(
                    select(Approval).where(Approval.user_id == user_id)
                )
            ).scalar_one_or_none() is None


class TestApprovalUniqueUser:
    """`user_id` is unique: one approval row per user, period."""

    async def test_duplicate_user_approval_raises_integrity_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            user_id = await _make_user(session, "erin")
            session.add(Approval(user_id=user_id))
            await session.commit()

        async with session_factory() as session:
            session.add(Approval(user_id=user_id))
            with pytest.raises(IntegrityError):
                await session.commit()