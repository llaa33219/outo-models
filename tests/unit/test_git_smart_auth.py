"""Unit tests for `outo_models.git_smart.auth`.

The auth module owns two contracts:

    1. `resolve_git_identity` — parse an HTTP Basic header, look up the user,
       and verify the password-as-PAT against every stored fingerprint.
       Returns the matched `User` or `None`; never raises.
    2. `authorize` — apply the (PULL / PUSH) x (public / private) decision
       matrix for a given `(repo, owner, user)` triple.

Each test owns its own engine + schema via the `session_factory` fixture
pattern used elsewhere in the suite.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    Base,
    PersonalAccessToken,
    Repo,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import ForbiddenError, UnauthorizedError
from outo_models.git_smart.auth import GitAction, authorize, resolve_git_identity
from outo_models.repos.models import RepoKind, Visibility


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


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    username: str,
    *,
    role: str = "user",
    status: str = "approved",
) -> User:
    """Insert a user with the given role/status; return the fresh ORM row."""
    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="h",
            role=role,
            status=status,
        )
        session.add(user)
        await session.commit()
        return (await session.execute(select(User).where(User.username == username))).scalar_one()


async def _seed_repo(
    factory: async_sessionmaker[AsyncSession],
    owner: User,
    name: str = "model-a",
    visibility: Visibility = Visibility.PRIVATE,
) -> Repo:
    async with factory() as session:
        repo = Repo(
            owner_id=owner.id,
            name=name,
            kind=RepoKind.MODEL.value,
            visibility=visibility.value,
            default_branch="main",
            size_bytes=0,
            path=f"{owner.username}/{name}.git",
        )
        session.add(repo)
        await session.commit()
        return (await session.execute(select(Repo).where(Repo.id == repo.id))).scalar_one()


async def _mint_pat(
    factory: async_sessionmaker[AsyncSession],
    user: User,
    raw_token: str,
    *,
    expires_at: datetime | None = None,
) -> PersonalAccessToken:
    """Insert a PAT row whose fingerprint matches `raw_token`."""
    async with factory() as session:
        pat = PersonalAccessToken(
            user_id=user.id,
            name=f"token-for-{user.username}",
            fingerprint_hash=fingerprint(raw_token),
            prefix=raw_token[:8],
            scopes='["read","write"]',
            expires_at=expires_at,
        )
        session.add(pat)
        await session.commit()
        return (
            await session.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.id == pat.id)
            )
        ).scalar_one()


def _basic(username: str, password: str) -> str:
    """Build an `Authorization: Basic …` header value (no scheme prefix)."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


class TestResolveGitIdentityMissingHeader:
    """Missing / malformed Authorization headers resolve to `None`."""

    async def test_none_header_returns_none(self, tmp_data_dir: Path) -> None:
        assert await resolve_git_identity(None, settings=get_settings()) is None

    async def test_empty_header_returns_none(self, tmp_data_dir: Path) -> None:
        assert await resolve_git_identity("", settings=get_settings()) is None

    async def test_non_basic_scheme_returns_none(self, tmp_data_dir: Path) -> None:
        assert await resolve_git_identity("Bearer some-token", settings=get_settings()) is None

    async def test_malformed_base64_returns_none(self, tmp_data_dir: Path) -> None:
        # `Basic !!!` is not valid base64 → must not raise.
        assert await resolve_git_identity("Basic !!!notbase64", settings=get_settings()) is None

    async def test_basic_without_colon_returns_none(self, tmp_data_dir: Path) -> None:
        # No colon separator → cannot split user/password.
        encoded = base64.b64encode(b"nocolonhere").decode("ascii")
        assert await resolve_git_identity(f"Basic {encoded}", settings=get_settings()) is None


class TestResolveGitIdentityUserLookup:
    """The username portion of the header must match a stored user."""

    async def test_unknown_user_returns_none(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        header = _basic("ghost", "anything")
        assert await resolve_git_identity(header, settings=get_settings()) is None

    async def test_user_exists_but_no_tokens_returns_none(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_user(session_factory, "alice")
        header = _basic("alice", "some-pat-value")
        assert await resolve_git_identity(header, settings=get_settings()) is None

    async def test_password_containing_colon_parses_correctly(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # PATs are opaque strings; a colon inside must NOT split the
        # username away from the password — `base64.split(":", 1)` is the
        # only correct parser.
        user = await _seed_user(session_factory, "alice")
        pat_value = "v4.local.abc:def:ghi"  # multiple colons
        await _mint_pat(session_factory, user, pat_value)

        header = _basic("alice", pat_value)
        resolved = await resolve_git_identity(header, settings=get_settings())
        assert resolved is not None
        assert resolved.id == user.id


class TestResolveGitIdentityMatching:
    """A correct PAT yields the matching `User`; mismatches yield `None`."""

    async def test_correct_pat_returns_user(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _seed_user(session_factory, "alice")
        token = "v4.local.alice-secret-token"
        await _mint_pat(session_factory, user, token)

        header = _basic("alice", token)
        resolved = await resolve_git_identity(header, settings=get_settings())
        assert resolved is not None
        assert resolved.id == user.id
        assert resolved.username == "alice"

    async def test_wrong_pat_returns_none(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _seed_user(session_factory, "alice")
        await _mint_pat(session_factory, user, "v4.local.real-token")

        header = _basic("alice", "v4.local.WRONG-token")
        assert await resolve_git_identity(header, settings=get_settings()) is None

    async def test_expired_token_returns_none(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _seed_user(session_factory, "alice")
        token = "v4.local.expired-token"
        past = datetime.now(tz=UTC) - timedelta(seconds=1)
        await _mint_pat(session_factory, user, token, expires_at=past)

        header = _basic("alice", token)
        assert await resolve_git_identity(header, settings=get_settings()) is None

    async def test_picks_the_matching_token_among_many(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _seed_user(session_factory, "alice")
        await _mint_pat(session_factory, user, "v4.local.t1")
        await _mint_pat(session_factory, user, "v4.local.t2")
        await _mint_pat(session_factory, user, "v4.local.t3")

        header = _basic("alice", "v4.local.t2")
        resolved = await resolve_git_identity(header, settings=get_settings())
        assert resolved is not None
        assert resolved.id == user.id

    async def test_updates_last_used_at_on_match(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _seed_user(session_factory, "alice")
        token = "v4.local.alice-usage-token"
        pat = await _mint_pat(session_factory, user, token)
        assert pat.last_used_at is None

        await resolve_git_identity(_basic("alice", token), settings=get_settings())

        async with session_factory() as session:
            refreshed = await session.get(PersonalAccessToken, pat.id)
            assert refreshed is not None
            assert refreshed.last_used_at is not None


class TestAuthorizePull:
    """Pull-time decisions follow the public-reads-free, private-needs-auth rule."""

    async def test_anonymous_pull_on_public_is_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        # No user passed — must not raise.
        await authorize(None, repo=repo, owner=owner, action=GitAction.PULL)

    async def test_random_user_pull_on_public_is_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        viewer = await _seed_user(session_factory, "bob")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        await authorize(viewer, repo=repo, owner=owner, action=GitAction.PULL)

    async def test_anonymous_pull_on_private_is_unauthorized(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PRIVATE)
        with pytest.raises(UnauthorizedError):
            await authorize(None, repo=repo, owner=owner, action=GitAction.PULL)

    async def test_owner_pull_on_private_is_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PRIVATE)
        await authorize(owner, repo=repo, owner=owner, action=GitAction.PULL)

    async def test_other_user_pull_on_private_is_forbidden(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        viewer = await _seed_user(session_factory, "bob")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PRIVATE)
        with pytest.raises(ForbiddenError):
            await authorize(viewer, repo=repo, owner=owner, action=GitAction.PULL)

    async def test_admin_pull_on_private_is_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        admin = await _seed_user(session_factory, "admin1", role="admin")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PRIVATE)
        await authorize(admin, repo=repo, owner=owner, action=GitAction.PULL)


class TestAuthorizePush:
    """Push always requires an authenticated owner or admin."""

    async def test_anonymous_push_is_unauthorized(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        with pytest.raises(UnauthorizedError):
            await authorize(None, repo=repo, owner=owner, action=GitAction.PUSH)

    async def test_non_owner_push_is_forbidden_even_on_public(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        intruder = await _seed_user(session_factory, "mallory")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        with pytest.raises(ForbiddenError):
            await authorize(intruder, repo=repo, owner=owner, action=GitAction.PUSH)

    async def test_owner_push_is_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        await authorize(owner, repo=repo, owner=owner, action=GitAction.PUSH)

    async def test_admin_push_is_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice")
        admin = await _seed_user(session_factory, "admin1", role="admin")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PRIVATE)
        await authorize(admin, repo=repo, owner=owner, action=GitAction.PUSH)


class TestAuthorizeInactiveUser:
    """A banned / pending user is forbidden from any privileged action."""

    async def test_banned_user_push_is_forbidden(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice", status="banned")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        with pytest.raises(ForbiddenError):
            await authorize(owner, repo=repo, owner=owner, action=GitAction.PUSH)

    async def test_pending_owner_cannot_push(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _seed_user(session_factory, "alice", status="pending")
        repo = await _seed_repo(session_factory, owner, visibility=Visibility.PUBLIC)
        with pytest.raises(ForbiddenError):
            await authorize(owner, repo=repo, owner=owner, action=GitAction.PUSH)


class TestGitActionEnum:
    """`GitAction` exposes `PULL` and `PUSH` with stable string values."""

    def test_values(self) -> None:
        assert GitAction.PULL == "pull"
        assert GitAction.PUSH == "push"
