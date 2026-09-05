"""Unit tests for `outo_models.repos.social` (likes, follows, comments).

The contract each test pins:

    * `like_repo` / `unlike_repo` are idempotent — a second call returns
      `False` without raising.
    * `like_count` reflects the row count for the repo.
    * `is_following` / `follower_count` mirror the like / count pair.
    * Self-follow raises `ForbiddenError` (the DB CHECK is a backstop).
    * Comment body length and non-blank rules are enforced at the
      service layer so the API does not duplicate the check.
    * Every mutation writes an `AuditLog` row.

# allow: SIZE_OK — the v0.3.0 ownership list specifies a single
# `tests/unit/test_social.py` covering all three social surfaces.
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
    RepoComment,
    RepoLike,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import ForbiddenError, ValidationFailedError
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility
from outo_models.repos.social import (
    add_comment,
    follow_user,
    follower_count,
    is_following,
    is_liked,
    like_count,
    like_repo,
    list_comments,
    load_repo_or_404,
    unfollow_user,
    unlike_repo,
)


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed_two_users(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[User, User]:
    alice = User(username="alice", email="alice@example.com", password_hash="h", status="approved")
    bob = User(username="bob", email="bob@example.com", password_hash="h", status="approved")
    async with factory() as session:
        session.add_all([alice, bob])
        await session.commit()
        alice_id = alice.id
        bob_id = bob.id
    async with factory() as session:
        a = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
        b = (await session.execute(select(User).where(User.id == bob_id))).scalar_one()
        return a, b


async def _seed_repo(factory: async_sessionmaker[AsyncSession], owner: User) -> Repo:
    async with factory() as session:
        owner_fk = (await session.execute(select(User).where(User.id == owner.id))).scalar_one()
        await create_repo(
            session,
            owner=owner_fk,
            name="model-a",
            kind=RepoKind.MODEL,
            visibility=Visibility.PUBLIC,
        )
        await session.commit()
    async with factory() as session:
        return await load_repo_or_404(session, owner=owner.username, name="model-a")


class TestLikeRepo:
    async def test_first_like_inserts_and_audits(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            inserted = await like_repo(session, user=bob, repo=repo)
            await session.commit()
            assert inserted is True
            assert await is_liked(session, user=bob, repo=repo) is True
            assert await like_count(session, repo=repo) == 1

            audit = (
                await session.execute(select(AuditLog).where(AuditLog.action == "repo.like"))
            ).scalar_one()
            assert audit.actor_id == bob.id
            assert audit.target_id == str(repo.id)

    async def test_second_like_is_idempotent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            assert await like_repo(session, user=bob, repo=repo) is True
            await session.commit()

        async with session_factory() as session:
            assert await like_repo(session, user=bob, repo=repo) is False
            await session.commit()

        async with session_factory() as session:
            rows = (
                (await session.execute(select(RepoLike).where(RepoLike.repo_id == repo.id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert await like_count(session, repo=repo) == 1

    async def test_unlike_is_idempotent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            await unlike_repo(session, user=bob, repo=repo)  # not liked yet
            await session.commit()

        async with session_factory() as session:
            assert await like_repo(session, user=bob, repo=repo) is True
            await session.commit()

        async with session_factory() as session:
            assert await unlike_repo(session, user=bob, repo=repo) is True
            await session.commit()
            assert await unlike_repo(session, user=bob, repo=repo) is False  # idempotent
            await session.commit()
            assert await like_count(session, repo=repo) == 0

    async def test_like_count_distinguishes_users(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            await like_repo(session, user=alice, repo=repo)
            await like_repo(session, user=bob, repo=repo)
            await session.commit()
            assert await like_count(session, repo=repo) == 2
            assert await is_liked(session, user=alice, repo=repo) is True
            assert await is_liked(session, user=bob, repo=repo) is True


class TestFollowUser:
    async def test_follow_inserts_and_counts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)

        async with session_factory() as session:
            assert await follow_user(session, follower=alice, followee=bob) is True
            await session.commit()
            assert await is_following(session, follower=alice, followee=bob) is True
            assert await follower_count(session, followee=bob) == 1

    async def test_follow_idempotent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)

        async with session_factory() as session:
            assert await follow_user(session, follower=alice, followee=bob) is True
            await session.commit()
            assert await follow_user(session, follower=alice, followee=bob) is False
            await session.commit()
            assert await follower_count(session, followee=bob) == 1

    async def test_self_follow_raises_forbidden(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, _bob = await _seed_two_users(session_factory)

        async with session_factory() as session:
            with pytest.raises(ForbiddenError):
                await follow_user(session, follower=alice, followee=alice)

    async def test_unfollow_idemp(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        alice, bob = await _seed_two_users(session_factory)

        async with session_factory() as session:
            await follow_user(session, follower=alice, followee=bob)
            await session.commit()

        async with session_factory() as session:
            assert await unfollow_user(session, follower=alice, followee=bob) is True
            await session.commit()
            assert await unfollow_user(session, follower=alice, followee=bob) is False
            await session.commit()
            assert await follower_count(session, followee=bob) == 0

    async def test_follow_writes_audit(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)

        async with session_factory() as session:
            await follow_user(session, follower=alice, followee=bob)
            await session.commit()
            audit = (
                await session.execute(select(AuditLog).where(AuditLog.action == "user.follow"))
            ).scalar_one()
            assert audit.actor_id == alice.id
            assert audit.target_id == str(bob.id)
            detail = audit.detail or ""
            assert "alice" in detail
            assert "bob" in detail


class TestComments:
    async def test_add_comment_persists_and_audits(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            comment = await add_comment(session, author=bob, repo=repo, body="looks great")
            await session.commit()
            assert comment.id > 0
            assert comment.body == "looks great"

        async with session_factory() as session:
            rows = (
                (await session.execute(select(RepoComment).where(RepoComment.repo_id == repo.id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            audit = (
                await session.execute(select(AuditLog).where(AuditLog.action == "repo.comment"))
            ).scalar_one()
            assert audit.actor_id == bob.id

    async def test_blank_comment_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            with pytest.raises(ValidationFailedError):
                await add_comment(session, author=bob, repo=repo, body="   \n  ")

    async def test_too_long_comment_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            with pytest.raises(ValidationFailedError):
                await add_comment(session, author=bob, repo=repo, body="x" * 4001)

    async def test_list_comments_newest_first_with_author(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            await add_comment(session, author=alice, repo=repo, body="first")
            await session.commit()

        async with session_factory() as session:
            await add_comment(session, author=bob, repo=repo, body="second")
            await session.commit()

        async with session_factory() as session:
            rows = await list_comments(session, repo=repo)
            assert [r.body for r in rows] == ["second", "first"]
            assert rows[0].author.username == "bob"
            assert rows[1].author.username == "alice"

    async def test_list_comments_respects_limit_and_offset(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        alice, _bob = await _seed_two_users(session_factory)
        repo = await _seed_repo(session_factory, alice)

        async with session_factory() as session:
            for i in range(5):
                await add_comment(session, author=alice, repo=repo, body=f"c{i}")
            await session.commit()

        async with session_factory() as session:
            first_page = await list_comments(session, repo=repo, limit=2, offset=0)
            second_page = await list_comments(session, repo=repo, limit=2, offset=2)
        assert [r.body for r in first_page] == ["c4", "c3"]
        assert [r.body for r in second_page] == ["c2", "c1"]
