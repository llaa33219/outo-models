"""Round-trip tests for the `Revision` ORM model.

Covers create / read / update / delete and the FK constraints to `repos`
and (nullable) `users`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    Repo,
    Revision,
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


async def _make_user_and_repo(session: AsyncSession) -> tuple[int, int]:
    session.add(
        User(
            username="author",
            email="author@example.com",
            password_hash="h",
        )
    )
    await session.commit()
    owner_id = (
        await session.execute(select(User.id).where(User.username == "author"))
    ).scalar_one()
    session.add(
        Repo(
            owner_id=owner_id,
            name="r1",
            kind="model",
            path="author/r1.git",
        )
    )
    await session.commit()
    repo_id = (await session.execute(select(Repo.id).where(Repo.name == "r1"))).scalar_one()
    return owner_id, repo_id


class TestRevisionCreateRead:
    """Revision rows round-trip with FK fields populated."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            _owner_id, repo_id = await _make_user_and_repo(session)
            session.add(
                Revision(
                    repo_id=repo_id,
                    commit_sha="a" * 40,
                    branch="main",
                    message="initial commit",
                    size_bytes=128,
                )
            )
            await session.commit()

        async with session_factory() as session:
            rev = (
                await session.execute(select(Revision).where(Revision.commit_sha == "a" * 40))
            ).scalar_one()
            assert rev.repo_id == repo_id
            assert rev.branch == "main"
            assert rev.message == "initial commit"
            assert rev.size_bytes == 128
            assert rev.author_id is None

    async def test_create_with_author(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id, repo_id = await _make_user_and_repo(session)
            session.add(
                Revision(
                    repo_id=repo_id,
                    commit_sha="b" * 40,
                    branch="feature",
                    author_id=owner_id,
                    message="second commit",
                )
            )
            await session.commit()

        async with session_factory() as session:
            rev = (
                await session.execute(select(Revision).where(Revision.commit_sha == "b" * 40))
            ).scalar_one()
            assert rev.author_id == owner_id
            assert rev.branch == "feature"


class TestRevisionUpdate:
    """`message` / `size_bytes` are mutable; PK is immutable."""

    async def test_update_message(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            _owner_id, repo_id = await _make_user_and_repo(session)
            session.add(
                Revision(
                    repo_id=repo_id,
                    commit_sha="c" * 40,
                    message="initial",
                )
            )
            await session.commit()
            rev_id = (
                await session.execute(select(Revision.id).where(Revision.commit_sha == "c" * 40))
            ).scalar_one()

        async with session_factory() as session:
            rev = await session.get(Revision, rev_id)
            assert rev is not None
            rev.message = "amended commit message"
            rev.size_bytes = 256
            await session.commit()

        async with session_factory() as session:
            rev = await session.get(Revision, rev_id)
            assert rev is not None
            assert rev.message == "amended commit message"
            assert rev.size_bytes == 256


class TestRevisionDelete:
    """Delete removes the row."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            _owner_id, repo_id = await _make_user_and_repo(session)
            session.add(
                Revision(
                    repo_id=repo_id,
                    commit_sha="d" * 40,
                    message="drop me",
                )
            )
            await session.commit()
            rev_id = (
                await session.execute(select(Revision.id).where(Revision.commit_sha == "d" * 40))
            ).scalar_one()

        async with session_factory() as session:
            rev = await session.get(Revision, rev_id)
            assert rev is not None
            await session.delete(rev)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(select(Revision).where(Revision.commit_sha == "d" * 40))
            ).scalar_one_or_none() is None


class TestRevisionForeignKeys:
    """FKs point at the correct rows on read."""

    async def test_author_fk_can_be_set_and_unset(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            _owner_id, repo_id = await _make_user_and_repo(session)
            session.add(
                Revision(
                    repo_id=repo_id,
                    commit_sha="e" * 40,
                    message="m",
                )
            )
            await session.commit()
            rev_id = (
                await session.execute(select(Revision.id).where(Revision.commit_sha == "e" * 40))
            ).scalar_one()

        async with session_factory() as session:
            rev = await session.get(Revision, rev_id)
            assert rev is not None
            assert rev.author_id is None

        async with session_factory() as session:
            owner_id = (
                await session.execute(select(User.id).where(User.username == "author"))
            ).scalar_one()
            rev = await session.get(Revision, rev_id)
            assert rev is not None
            rev.author_id = owner_id
            await session.commit()

        async with session_factory() as session:
            rev = await session.get(Revision, rev_id)
            assert rev is not None
            assert rev.author_id == owner_id
