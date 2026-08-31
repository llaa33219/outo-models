"""Round-trip tests for the `PersonalAccessToken` ORM model.

Covers create / read / update / delete, the unique constraint on
`fingerprint_hash`, and the `is_expired` property that gates auth.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    Base,
    PersonalAccessToken,
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


class TestTokenCreateRead:
    """Token rows survive commit and round-trip with their hashed fingerprint."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "alice")
            raw = "v4.local.realtokenvalue"
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="ci-token",
                    fingerprint_hash=fingerprint(raw),
                    prefix=raw[:8],
                    scopes='["read","write"]',
                )
            )
            await session.commit()

        async with session_factory() as session:
            token = (
                await session.execute(
                    select(PersonalAccessToken).where(
                        PersonalAccessToken.name == "ci-token"
                    )
                )
            ).scalar_one()
            assert token.user_id == owner_id
            assert token.prefix == "v4.local"
            assert token.scopes == '["read","write"]'
            assert token.fingerprint_hash.startswith("$argon2id$")
            assert token.expires_at is None
            assert token.last_used_at is None

    async def test_create_with_expiry(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        expires_at = datetime.now(tz=UTC) + timedelta(days=30)
        async with session_factory() as session:
            owner_id = await _make_user(session, "bob")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="expiring",
                    fingerprint_hash=fingerprint("v4.local.bob-token"),
                    prefix="v4.local",
                    scopes='["read"]',
                    expires_at=expires_at,
                )
            )
            await session.commit()

        async with session_factory() as session:
            token = (
                await session.execute(
                    select(PersonalAccessToken).where(
                        PersonalAccessToken.name == "expiring"
                    )
                )
            ).scalar_one()
            assert token.expires_at is not None


class TestTokenUpdate:
    """`last_used_at` is mutable; `fingerprint_hash` is not (would violate unique)."""

    async def test_update_last_used_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "carol")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="usage-tracking",
                    fingerprint_hash=fingerprint("v4.local.carol-token"),
                    prefix="v4.local",
                    scopes='["read"]',
                )
            )
            await session.commit()
            token_id = (
                await session.execute(
                    select(PersonalAccessToken.id).where(
                        PersonalAccessToken.name == "usage-tracking"
                    )
                )
            ).scalar_one()

        last_used = datetime.now(tz=UTC) + timedelta(seconds=1)
        async with session_factory() as session:
            token = await session.get(PersonalAccessToken, token_id)
            assert token is not None
            token.last_used_at = last_used
            await session.commit()

        async with session_factory() as session:
            token = await session.get(PersonalAccessToken, token_id)
            assert token is not None
            assert token.last_used_at is not None


class TestTokenDelete:
    """Delete removes the row."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "dave")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="disposable",
                    fingerprint_hash=fingerprint("v4.local.dave-token"),
                    prefix="v4.local",
                    scopes='["read"]',
                )
            )
            await session.commit()
            token_id = (
                await session.execute(
                    select(PersonalAccessToken.id).where(
                        PersonalAccessToken.name == "disposable"
                    )
                )
            ).scalar_one()

        async with session_factory() as session:
            token = await session.get(PersonalAccessToken, token_id)
            assert token is not None
            await session.delete(token)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(
                    select(PersonalAccessToken).where(
                        PersonalAccessToken.name == "disposable"
                    )
                )
            ).scalar_one_or_none() is None


class TestTokenUniqueFingerprint:
    """Two tokens with the same fingerprint cannot coexist."""

    async def test_duplicate_fingerprint_raises_integrity_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "erin")
            same_hash = fingerprint("v4.local.same-token")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="first",
                    fingerprint_hash=same_hash,
                    prefix="v4.local",
                    scopes='["read"]',
                )
            )
            await session.commit()

        async with session_factory() as session:
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="second",
                    fingerprint_hash=same_hash,
                    prefix="v4.local",
                    scopes='["read"]',
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


class TestTokenIsExpired:
    """`is_expired` returns False when no expiry is set, True for past, False for future."""

    async def test_no_expiry_is_not_expired(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            owner_id = await _make_user(session, "frank")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="forever",
                    fingerprint_hash=fingerprint("v4.local.frank-token"),
                    prefix="v4.local",
                    scopes='["read"]',
                )
            )
            await session.commit()

        async with session_factory() as session:
            token = (
                await session.execute(
                    select(PersonalAccessToken).where(
                        PersonalAccessToken.name == "forever"
                    )
                )
            ).scalar_one()
            assert token.is_expired is False

    async def test_past_expiry_is_expired(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        past = datetime.now(tz=UTC) - timedelta(seconds=1)
        async with session_factory() as session:
            owner_id = await _make_user(session, "greg")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="old",
                    fingerprint_hash=fingerprint("v4.local.greg-token"),
                    prefix="v4.local",
                    scopes='["read"]',
                    expires_at=past,
                )
            )
            await session.commit()

        async with session_factory() as session:
            token = (
                await session.execute(
                    select(PersonalAccessToken).where(
                        PersonalAccessToken.name == "old"
                    )
                )
            ).scalar_one()
            assert token.is_expired is True

    async def test_future_expiry_is_not_expired(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        future = datetime.now(tz=UTC) + timedelta(days=30)
        async with session_factory() as session:
            owner_id = await _make_user(session, "harry")
            session.add(
                PersonalAccessToken(
                    user_id=owner_id,
                    name="fresh",
                    fingerprint_hash=fingerprint("v4.local.harry-token"),
                    prefix="v4.local",
                    scopes='["read"]',
                    expires_at=future,
                )
            )
            await session.commit()

        async with session_factory() as session:
            token = (
                await session.execute(
                    select(PersonalAccessToken).where(
                        PersonalAccessToken.name == "fresh"
                    )
                )
            ).scalar_one()
            assert token.is_expired is False