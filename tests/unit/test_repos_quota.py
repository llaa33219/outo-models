"""Unit tests for `outo_models.repos.quota`.

`ensure_quota_rows`, `check_push_allowed`, `add_usage`, and `reconcile_user`
are the four operations WP-13 routers and the WP-11 scheduler depend on;
together they are the storage-accounting surface of the system.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    Repo,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import QuotaExceededError
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind
from outo_models.repos.quota import (
    add_usage,
    check_push_allowed,
    ensure_quota_rows,
    reconcile_user,
)


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed_user(factory: async_sessionmaker[AsyncSession], username: str) -> User:
    """Create a user and return the live ORM object from a fresh session."""
    async with factory() as session:
        session.add(User(username=username, email=f"{username}@example.com", password_hash="h"))
        await session.commit()
        return (await session.execute(select(User).where(User.username == username))).scalar_one()


async def _usage(factory: async_sessionmaker[AsyncSession], user_id: int) -> UserUsage:
    """Fetch the current `UserUsage` row for `user_id`."""
    async with factory() as session:
        return (
            await session.execute(select(UserUsage).where(UserUsage.user_id == user_id))
        ).scalar_one()


class TestEnsureQuotaRows:
    """`ensure_quota_rows` materializes defaults for fresh accounts."""

    async def test_creates_both_rows_for_a_new_user(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        async with session_factory() as session:
            quota, usage = await ensure_quota_rows(session, owner)
            await session.commit()
        assert quota.max_bytes == get_settings().default_quota_bytes
        assert usage.used_bytes == 0

    async def test_is_idempotent_for_existing_rows(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _seed_user(session_factory, "bob")
            session.add(UserQuota(user_id=owner.id, max_bytes=42))
            session.add(UserUsage(user_id=owner.id, used_bytes=7))
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            quota, usage = await ensure_quota_rows(session, owner)
            await session.commit()
        assert quota.max_bytes == 42
        assert usage.used_bytes == 7


class TestCheckPushAllowed:
    """`check_push_allowed` raises when the cap would be exceeded."""

    async def test_passes_with_room_to_grow(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "carol")
        async with session_factory() as session:
            await check_push_allowed(session, owner, 1024)  # does not raise
            await session.commit()

    async def test_raises_when_would_exceed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "dave")
        async with session_factory() as session:
            # Pin the cap so the math is independent of `default_quota_bytes`.
            await ensure_quota_rows(session, owner)
            quota = (
                await session.execute(select(UserQuota).where(UserQuota.user_id == owner.id))
            ).scalar_one()
            quota.max_bytes = 1000
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            with pytest.raises(QuotaExceededError):
                await check_push_allowed(session, owner, 1500)
            await session.commit()

    @pytest.mark.parametrize("delta", [-(10**9), 0], ids=["negative", "zero"])
    async def test_non_positive_delta_always_passes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        delta: int,
    ) -> None:
        owner = await _seed_user(session_factory, f"x-{delta}")
        async with session_factory() as session:
            await check_push_allowed(session, owner, delta)  # does not raise
            await session.commit()

    async def test_exact_cap_passes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "greg")
        async with session_factory() as session:
            await ensure_quota_rows(session, owner)
            quota = (
                await session.execute(select(UserQuota).where(UserQuota.user_id == owner.id))
            ).scalar_one()
            quota.max_bytes = 1000
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            # Boundary: equal to cap is allowed (the rule is `>` not `>=`).
            await check_push_allowed(session, owner, 1000)
            await session.commit()


class TestAddUsage:
    """`add_usage` adjusts the tally and clamps at zero."""

    async def test_positive_delta_increments(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "harry")
        async with session_factory() as session:
            await add_usage(session, owner, 4096)
            await session.commit()
        assert (await _usage(session_factory, owner.id)).used_bytes == 4096

    async def test_negative_delta_decrements(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "ivy")
        async with session_factory() as session:
            await add_usage(session, owner, 4096)
            await add_usage(session, owner, -2048)
            await session.commit()
        assert (await _usage(session_factory, owner.id)).used_bytes == 2048

    async def test_clamps_at_zero(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        owner = await _seed_user(session_factory, "jane")
        async with session_factory() as session:
            await add_usage(session, owner, -1000)  # no positive to subtract
            await session.commit()
        assert (await _usage(session_factory, owner.id)).used_bytes == 0


class TestReconcileUser:
    """`reconcile_user` rebuilds `used_bytes` from the on-disk truth."""

    async def test_sums_actual_disk_usage(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        owner = await _seed_user(session_factory, "kate")
        async with session_factory() as session:
            owner_obj = (
                await session.execute(select(User).where(User.id == owner.id))
            ).scalar_one()
            await create_repo(session, owner=owner_obj, name="m1", kind=RepoKind.MODEL)
            await create_repo(session, owner=owner_obj, name="m2", kind=RepoKind.MODEL)
            await reconcile_user(session, owner_obj)
            await session.commit()

        # Establish a baseline first; the fresh bare repo overhead varies
        # across dulwich versions so we anchor on the *delta* we add here.
        baseline = (await _usage(session_factory, owner.id)).used_bytes

        for name, payload in (("m1", b"x" * 100), ("m2", b"y" * 250)):
            target = tmp_data_dir / "repos" / "kate" / f"{name}.git" / "marker"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

        async with session_factory() as session:
            owner_obj = (
                await session.execute(select(User).where(User.id == owner.id))
            ).scalar_one()
            drift = await reconcile_user(session, owner_obj)
            await session.commit()
            assert drift == 350

        assert (await _usage(session_factory, owner.id)).used_bytes == baseline + 350

    async def test_corrections_are_returned_as_drift(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _seed_user(session_factory, "liam")
            # Pretend the DB lies about a never-created repo.
            session.add(
                Repo(
                    owner_id=owner.id,
                    name="ghost",
                    kind="model",
                    path="liam/ghost.git",
                )
            )
            session.add(UserUsage(user_id=owner.id, used_bytes=9999))
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner_obj = (
                await session.execute(select(User).where(User.id == owner_id))
            ).scalar_one()
            drift = await reconcile_user(session, owner_obj)
            await session.commit()
            # 9999 -> 0 (ghost repo has zero bytes on disk).
            assert drift == -9999
        assert (await _usage(session_factory, owner_id)).used_bytes == 0

    async def test_creates_usage_row_when_missing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "mia")
        async with session_factory() as session:
            owner_obj = (
                await session.execute(select(User).where(User.id == owner.id))
            ).scalar_one()
            drift = await reconcile_user(session, owner_obj)
            await session.commit()
            assert drift == 0
        assert (await _usage(session_factory, owner.id)).used_bytes == 0
