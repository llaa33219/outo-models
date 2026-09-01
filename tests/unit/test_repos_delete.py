"""Unit tests for `outo_models.repos.delete.delete_repo`.

`delete_repo` removes a bare repo from disk, drops its `Repo` and `Revision`
rows, decrements the owner's `UserUsage.used_bytes`, and appends an audit
entry — all in a single transaction that the caller commits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    AuditLog,
    Base,
    Repo,
    Revision,
    User,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import NotFoundError
from outo_models.repos.create import create_repo
from outo_models.repos.delete import delete_repo
from outo_models.repos.models import RepoKind
from outo_models.repos.storage import repo_exists, repo_fs_path


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


async def _make_user(session: AsyncSession, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", password_hash="h")
    session.add(user)
    await session.commit()
    return (await session.execute(select(User).where(User.username == username))).scalar_one()


async def _seed_repo_with_revision(
    session: AsyncSession, owner: User, name: str, kind: RepoKind, *, size_bytes: int
) -> int:
    """Create a repo and a single revision row; return the `Repo.id`."""
    repo = await create_repo(session, owner=owner, name=name, kind=kind)
    # Simulate that a previous push has grown the repo.
    repo.size_bytes = size_bytes
    await session.flush()
    session.add(
        Revision(
            repo_id=repo.id,
            commit_sha="a" * 40,
            branch="main",
            author_id=owner.id,
            message="initial commit",
            size_bytes=size_bytes,
        )
    )
    await session.commit()
    return repo.id


class TestDeleteRepoSuccess:
    """Happy-path delete removes on-disk dir + every related DB row."""

    async def test_removes_row_dir_and_revision(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "alice")
            repo_id = await _seed_repo_with_revision(
                session, owner, "model-a", RepoKind.MODEL, size_bytes=4096
            )
            # Set a non-zero used_bytes so we can prove the decrement ran.
            from outo_models.db import UserUsage as _UserUsage

            usage_row = (
                await session.execute(select(_UserUsage).where(_UserUsage.user_id == owner.id))
            ).scalar_one()
            usage_row.used_bytes = 4096
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            await delete_repo(session, owner=owner, name="model-a", kind=RepoKind.MODEL)
            await session.commit()

        fs_path = repo_fs_path("alice", "model-a")
        assert not fs_path.exists()
        assert not repo_exists("alice", "model-a")

        async with session_factory() as session:
            repo_count = (
                await session.execute(select(Repo).where(Repo.id == repo_id))
            ).scalar_one_or_none()
            assert repo_count is None

            rev_count = (
                (await session.execute(select(Revision).where(Revision.repo_id == repo_id)))
                .scalars()
                .all()
            )
            assert rev_count == []

            usage = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            assert usage.used_bytes == 0

    async def test_appends_repo_delete_audit_entry(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "bob")
            await create_repo(session, owner=owner, name="ds-b", kind=RepoKind.DATASET)
            await session.commit()

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.username == "bob"))).scalar_one()
            await delete_repo(session, owner=owner, name="ds-b", kind=RepoKind.DATASET)
            await session.commit()

        async with session_factory() as session:
            entries = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "repo.delete")))
                .scalars()
                .all()
            )
            assert len(entries) == 1
            assert entries[0].target_type == "repo"

    async def test_used_bytes_floors_at_zero(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Pretend the DB got out of sync with the disk: recorded size is
        # larger than the current usage. The decrement must clamp at 0.
        async with session_factory() as session:
            owner = await _make_user(session, "carol")
            repo_id = await _seed_repo_with_revision(
                session, owner, "model-c", RepoKind.MODEL, size_bytes=10_000
            )
            from outo_models.db import UserUsage as _UserUsage

            usage_row = (
                await session.execute(select(_UserUsage).where(_UserUsage.user_id == owner.id))
            ).scalar_one()
            usage_row.used_bytes = 5_000  # less than recorded size
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.id == owner_id))).scalar_one()
            await delete_repo(session, owner=owner, name="model-c", kind=RepoKind.MODEL)
            await session.commit()

        async with session_factory() as session:
            usage = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner_id))
            ).scalar_one()
            assert usage.used_bytes == 0
            # Cleanup sanity: row really did disappear.
            row = (
                await session.execute(select(Repo).where(Repo.id == repo_id))
            ).scalar_one_or_none()
            assert row is None

    async def test_missing_dir_is_idempotent(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "dave")
            await create_repo(session, owner=owner, name="sp-d", kind=RepoKind.SPACE)
            await session.commit()

        # Wipe the on-disk dir before deleting from DB; the operation must
        # still complete cleanly instead of erroring on `shutil.rmtree`.
        import shutil

        shutil.rmtree(repo_fs_path("dave", "sp-d"))
        assert not repo_fs_path("dave", "sp-d").exists()

        async with session_factory() as session:
            owner = (
                await session.execute(select(User).where(User.username == "dave"))
            ).scalar_one()
            await delete_repo(session, owner=owner, name="sp-d", kind=RepoKind.SPACE)
            await session.commit()


class TestDeleteRepoMissing:
    """Deleting a non-existent repo surfaces `NotFoundError`."""

    async def test_missing_repo_raises_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "erin")
            with pytest.raises(NotFoundError):
                await delete_repo(session, owner=owner, name="nope", kind=RepoKind.MODEL)

    async def test_wrong_kind_raises_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "frank")
            await create_repo(session, owner=owner, name="x", kind=RepoKind.MODEL)
            await session.commit()

        async with session_factory() as session:
            owner = (
                await session.execute(select(User).where(User.username == "frank"))
            ).scalar_one()
            with pytest.raises(NotFoundError):
                await delete_repo(session, owner=owner, name="x", kind=RepoKind.DATASET)

    async def test_double_delete_raises_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "greg")
            await create_repo(session, owner=owner, name="model-g", kind=RepoKind.MODEL)
            await session.commit()

        async with session_factory() as session:
            owner = (
                await session.execute(select(User).where(User.username == "greg"))
            ).scalar_one()
            await delete_repo(session, owner=owner, name="model-g", kind=RepoKind.MODEL)
            await session.commit()

        async with session_factory() as session:
            owner = (
                await session.execute(select(User).where(User.username == "greg"))
            ).scalar_one()
            with pytest.raises(NotFoundError):
                await delete_repo(session, owner=owner, name="model-g", kind=RepoKind.MODEL)
