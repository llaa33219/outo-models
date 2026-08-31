"""Unit tests for `outo_models.repos.create.create_repo`.

`create_repo` owns three contracts:
    1. A fresh bare repo on disk with a `Repo` row that points at it.
    2. Quota rows (`UserQuota`, `UserUsage`) for the owner on first create.
    3. A compensating cleanup that removes the bare repo if the DB phase
       fails after the on-disk init.

Each test owns its own engine + schema via the `session_factory` fixture
pattern used elsewhere in the suite.
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
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import ConflictError, ValidationFailedError
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility
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
    return (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one()


async def _user_id(session: AsyncSession, username: str) -> int:
    """Return `User.id` after commit has expired in-memory ORM objects."""
    row = (
        await session.execute(select(User.id).where(User.username == username))
    ).one_or_none()
    assert row is not None, f"User {username!r} not found"
    return row[0]


class TestCreateRepoSuccess:
    """Happy-path: a new bare repo plus matching rows appear together."""

    async def test_creates_bare_repo_on_disk_and_db_row(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "alice")
            await create_repo(
                session, owner=owner, name="model-a", kind=RepoKind.MODEL
            )
            await session.commit()

        fs_path = repo_fs_path("alice", "model-a")
        assert fs_path.is_dir()
        # `dulwich.porcelain.init(bare=True)` writes `HEAD` at the root.
        assert (fs_path / "HEAD").exists()

        alice_id = await _user_id_in_new_session(session_factory, "alice")
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Repo).where(Repo.owner_id == alice_id)
                )
            ).scalar_one()
            assert row.name == "model-a"
            assert row.kind == "model"
            assert row.visibility == "private"
            assert row.default_branch == "main"
            assert row.size_bytes == 0
            assert row.path == "alice/model-a.git"

    async def test_path_is_relative_to_repos_dir(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "bob")
            await create_repo(session, owner=owner, name="ds-b", kind=RepoKind.DATASET)
            await session.commit()

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Repo).where(Repo.name == "ds-b")
                )
            ).scalar_one()
            # `tmp_data_dir / "repos"` is the repos_dir — `path` must be
            # relative so the row survives `OUTO_DATA_DIR` changes.
            assert row.path == "bob/ds-b.git"
            assert not Path(row.path).is_absolute()

    async def test_visibility_and_description_persist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "carol")
            await create_repo(
                session,
                owner=owner,
                name="sp-c",
                kind=RepoKind.SPACE,
                visibility=Visibility.PUBLIC,
                description="hello world",
            )
            await session.commit()

        async with session_factory() as session:
            row = (
                await session.execute(select(Repo).where(Repo.name == "sp-c"))
            ).scalar_one()
            assert row.kind == "space"
            assert row.visibility == "public"
            assert row.description == "hello world"

    async def test_creates_quota_rows_on_first_repo(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "dave")
            await create_repo(session, owner=owner, name="model-d", kind=RepoKind.MODEL)
            await session.commit()

        dave_id = await _user_id_in_new_session(session_factory, "dave")
        async with session_factory() as session:
            quota = (
                await session.execute(
                    select(UserQuota).where(UserQuota.user_id == dave_id)
                )
            ).scalar_one()
            usage = (
                await session.execute(
                    select(UserUsage).where(UserUsage.user_id == dave_id)
                )
            ).scalar_one()
            assert quota.max_bytes == get_settings().default_quota_bytes
            assert usage.used_bytes == 0

    async def test_appends_repo_create_audit_entry(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "erin")
            await create_repo(session, owner=owner, name="model-e", kind=RepoKind.MODEL)
            await session.commit()

        async with session_factory() as session:
            entry = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "repo.create")
                )
            ).scalar_one()
            assert entry.target_type == "repo"
            assert '"model-e"' in (entry.detail or "")

    async def test_does_not_recreate_existing_quota_rows(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Pre-seed an unusually high cap; the second create must not clobber it.
        async with session_factory() as session:
            owner = await _make_user(session, "frank")
            session.add(UserQuota(user_id=owner.id, max_bytes=999_999_999))
            session.add(UserUsage(user_id=owner.id, used_bytes=1234))
            await session.commit()
            owner_id = owner.id

        async with session_factory() as session:
            owner = (
                await session.execute(select(User).where(User.id == owner_id))
            ).scalar_one()
            await create_repo(session, owner=owner, name="model-f", kind=RepoKind.MODEL)
            await session.commit()

        async with session_factory() as session:
            quota = (
                await session.execute(
                    select(UserQuota).where(UserQuota.user_id == owner_id)
                )
            ).scalar_one()
            usage = (
                await session.execute(
                    select(UserUsage).where(UserUsage.user_id == owner_id)
                )
            ).scalar_one()
            assert quota.max_bytes == 999_999_999
            assert usage.used_bytes == 1234


class TestCreateRepoConflict:
    """Duplicate `(owner, kind, name)` raises `ConflictError`."""

    async def test_duplicate_same_kind_raises(
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
            with pytest.raises(ConflictError):
                await create_repo(
                    session, owner=owner, name="model-g", kind=RepoKind.MODEL
                )


class TestCreateRepoSlugValidation:
    """An invalid name fails fast, before any disk or DB writes happen."""

    async def test_invalid_slug_raises_validation_failed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner = await _make_user(session, "ivy")
            with pytest.raises(ValidationFailedError):
                await create_repo(
                    session, owner=owner, name="Bad Name!", kind=RepoKind.MODEL
                )
            await session.commit()

        # And no bare repo on disk.
        assert not repo_exists("ivy", "Bad Name!")


async def _user_id_in_new_session(
    factory: async_sessionmaker[AsyncSession], username: str
) -> int:
    """Look up `User.id` for `username` using a fresh session from `factory`."""
    async with factory() as session:
        row = (
            await session.execute(select(User.id).where(User.username == username))
        ).one_or_none()
        assert row is not None, f"User {username!r} not found"
        return row[0]
