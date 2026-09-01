"""End-to-end lifecycle test for the repository domain layer.

Walks a single repo through every public operation the WP-13 routers will
invoke: create, quota check, reconcile, reflog (empty + populated), and
delete. Each step asserts the on-disk and DB state the rest of the system
depends on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from dulwich import porcelain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    Repo,
    User,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import ConflictError, NotFoundError, QuotaExceededError
from outo_models.repos.create import create_repo
from outo_models.repos.delete import delete_repo
from outo_models.repos.models import RepoKind
from outo_models.repos.quota import (
    add_usage,
    check_push_allowed,
    ensure_quota_rows,
    reconcile_user,
)
from outo_models.repos.reflog import recent_revisions
from outo_models.repos.storage import repo_fs_path


@pytest.fixture
async def factory(
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


async def _seed_user(factory: async_sessionmaker[AsyncSession], username: str) -> int:
    """Create a user and return the `User.id`."""
    async with factory() as session:
        session.add(User(username=username, email=f"{username}@example.com", password_hash="h"))
        await session.commit()
        user_id = (
            await session.execute(select(User.id).where(User.username == username))
        ).scalar_one()
        return user_id


async def _get_user(factory: async_sessionmaker[AsyncSession], user_id: int) -> User:
    async with factory() as session:
        return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


class TestRepoLifecycle:
    """A single repo walking through create → quota → reflog → delete."""

    async def test_full_lifecycle(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        # 1. Create: model repo appears on disk as a bare repo.
        owner_id = await _seed_user(factory, "alice")
        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            repo = await create_repo(session, owner=owner, name="my-model", kind=RepoKind.MODEL)
            await session.commit()
            repo_id = repo.id

        fs_path = repo_fs_path("alice", "my-model")
        assert fs_path.is_dir()
        assert (fs_path / "HEAD").exists()

        # 2. Duplicate create → ConflictError (and no second dir appears).
        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            with pytest.raises(ConflictError):
                await create_repo(session, owner=owner, name="my-model", kind=RepoKind.MODEL)

        # 3. Push-size quota accounting: a too-big push is rejected, and
        #    bookkeeping clamps at zero even with a negative delta.
        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            quota, usage = await ensure_quota_rows(session, owner)
            quota.max_bytes = 1024
            await session.commit()

        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            with pytest.raises(QuotaExceededError):
                await check_push_allowed(session, owner, 2048)
            await add_usage(session, owner, 500)
            await session.commit()

        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            usage_after = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            assert usage_after.used_bytes == 500
            # Floor at zero: a huge negative delta must not produce negative.
            await add_usage(session, owner, -10_000)
            await session.commit()

        async with factory() as session:
            usage_floored = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            assert usage_floored.used_bytes == 0

        # 4. Reconcile corrects an artificial drift.
        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            usage = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            usage.used_bytes = 999_999  # obviously wrong
            await session.commit()

        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            drift = await reconcile_user(session, owner)
            await session.commit()
            assert drift < 0  # tallied down to the real on-disk value

        async with factory() as session:
            usage_now = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            assert usage_now.used_bytes >= 0
            assert usage_now.used_bytes == drift + 999_999

        # 5. Reflog is empty on a fresh repo, then non-empty after a push.
        assert await recent_revisions("alice", "my-model") == []

        work = tmp_data_dir / "_lifecycle_work"
        work.mkdir()
        porcelain.init(str(work), bare=False)
        (work / "README.md").write_text("first\n")
        porcelain.add(str(work), paths=[str(work / "README.md")])
        porcelain.commit(
            str(work),
            message=b"first commit",
            author=b"Tester <tester@example.com>",
            committer=b"Tester <tester@example.com>",
        )
        porcelain.push(
            str(work),
            str(fs_path),
            b"refs/heads/master:refs/heads/main",
            force=True,
        )

        revs = await recent_revisions("alice", "my-model")
        assert len(revs) == 1
        assert revs[0].message == "first commit"

        # 6. Bump the recorded size to prove delete decrements usage.
        async with factory() as session:
            row = (await session.execute(select(Repo).where(Repo.id == repo_id))).scalar_one()
            row.size_bytes = 4096
            await session.flush()
            owner_obj = (
                await session.execute(select(User).where(User.id == row.owner_id))
            ).scalar_one()
            await add_usage(session, owner_obj, 4096)
            await session.commit()

        # 7. Delete removes the row, the dir, and decrements usage.
        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            await delete_repo(session, owner=owner, name="my-model", kind=RepoKind.MODEL)
            await session.commit()

        assert not fs_path.exists()

        async with factory() as session:
            row = (
                await session.execute(select(Repo).where(Repo.id == repo_id))
            ).scalar_one_or_none()
            assert row is None
            usage_after_delete = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            # The +4096 from `add_usage` was exactly cancelled by the
            # -4096 from `delete_repo`'s size_bytes decrement.
            assert usage_after_delete.used_bytes == drift + 999_999

        # 8. Deleting again surfaces NotFoundError.
        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            with pytest.raises(NotFoundError):
                await delete_repo(session, owner=owner, name="my-model", kind=RepoKind.MODEL)


class TestCreateRepoCompensatingCleanup:
    """A DB failure after the bare-repo init leaves no orphan on disk."""

    async def test_on_disk_repo_is_removed_when_db_fails(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from outo_models.repos import create as create_mod

        owner_id = await _seed_user(factory, "bob")

        # Force the post-init DB phase to explode. The bare-repo init has
        # already succeeded at this point, so the compensating cleanup
        # path is the one being exercised.
        async def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic DB failure")

        monkeypatch.setattr(create_mod, "ensure_quota_rows", boom)

        async with factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            with pytest.raises(RuntimeError, match="synthetic DB failure"):
                await create_repo(session, owner=owner, name="doomed", kind=RepoKind.MODEL)

        fs_path = repo_fs_path("bob", "doomed")
        assert not fs_path.exists(), "compensating cleanup must remove bare repo"
        assert not (tmp_data_dir / "repos" / "bob").exists() or not any(
            (tmp_data_dir / "repos" / "bob").iterdir()
        ), "owner segment must not retain an empty dir"
