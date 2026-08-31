"""Round-trip tests for the `Repo` ORM model.

Covers create / read / update / delete, the `(owner_id, kind, name)`
unique constraint, and the FK to `users`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import Base, Repo, User, dispose_engines, get_engine, get_session_factory


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


class TestRepoCreateRead:
    """Repo rows survive a commit and round-trip through a fresh session."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "alice")
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="my-model",
                    kind="model",
                    description="A test model",
                    path="alice/my-model.git",
                )
            )
            await session.commit()

        async with session_factory() as session:
            repo = (
                await session.execute(select(Repo).where(Repo.name == "my-model"))
            ).scalar_one()
            assert repo.kind == "model"
            assert repo.visibility == "private"
            assert repo.default_branch == "main"
            assert repo.size_bytes == 0
            assert repo.description == "A test model"
            assert repo.path == "alice/my-model.git"

    async def test_explicit_visibility_and_branch_persist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "bob")
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="public-dataset",
                    kind="dataset",
                    visibility="public",
                    default_branch="develop",
                    path="bob/public-dataset.git",
                )
            )
            await session.commit()

        async with session_factory() as session:
            repo = (
                await session.execute(select(Repo).where(Repo.name == "public-dataset"))
            ).scalar_one()
            assert repo.visibility == "public"
            assert repo.default_branch == "develop"


class TestRepoUpdate:
    """Updates to mutable columns are visible after a fresh session."""

    async def test_update_description_and_size(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "carol")
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="repo-c",
                    kind="model",
                    path="carol/repo-c.git",
                )
            )
            await session.commit()
            repo_id = (
                await session.execute(select(Repo.id).where(Repo.name == "repo-c"))
            ).scalar_one()

        async with session_factory() as session:
            repo = await session.get(Repo, repo_id)
            assert repo is not None
            repo.description = "Updated description"
            repo.size_bytes = 4096
            await session.commit()

        async with session_factory() as session:
            repo = await session.get(Repo, repo_id)
            assert repo is not None
            assert repo.description == "Updated description"
            assert repo.size_bytes == 4096


class TestRepoDelete:
    """Delete removes the row so a follow-up SELECT finds nothing."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "dave")
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="repo-d",
                    kind="model",
                    path="dave/repo-d.git",
                )
            )
            await session.commit()
            repo_id = (
                await session.execute(select(Repo.id).where(Repo.name == "repo-d"))
            ).scalar_one()

        async with session_factory() as session:
            repo = await session.get(Repo, repo_id)
            assert repo is not None
            await session.delete(repo)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(select(Repo).where(Repo.name == "repo-d"))
            ).scalar_one_or_none() is None


class TestRepoUniqueConstraint:
    """`(owner_id, kind, name)` is unique; same name across kind or owner is allowed."""

    async def test_same_owner_same_kind_duplicate_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "erin")
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="shared",
                    kind="model",
                    path="erin/shared.git",
                )
            )
            await session.commit()

        async with session_factory() as session:
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="shared",
                    kind="model",
                    path="erin/shared-2.git",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_same_name_different_kind_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "frank")
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="shared",
                    kind="model",
                    path="frank/shared-model.git",
                )
            )
            session.add(
                Repo(
                    owner_id=owner_id,
                    name="shared",
                    kind="dataset",
                    path="frank/shared-dataset.git",
                )
            )
            await session.commit()  # must not raise

    async def test_same_name_same_kind_different_owner_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            alice_id = await _make_user(session, "greg")
            session.add(
                Repo(
                    owner_id=alice_id,
                    name="shared",
                    kind="model",
                    path="greg/shared.git",
                )
            )
            await session.commit()

        async with session_factory() as session:
            bob_id = await _make_user(session, "harry")
            session.add(
                Repo(
                    owner_id=bob_id,
                    name="shared",
                    kind="model",
                    path="harry/shared.git",
                )
            )
            await session.commit()  # must not raise