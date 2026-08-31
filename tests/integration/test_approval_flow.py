"""Integration tests for the signup-approval state machine service.

Covers the full `register_user → can_login → approve / deny → ban / unban`
flow against a real sqlite-backed engine, asserting both the public
state-machine contract and the audit log emitted on every transition.

The service layer owns one rule above all others: it never commits.
Every test below passes a session it controls, then commits explicitly so
the rollback-on-exception path is exercisable without coupling to the
production transaction wrapper.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.auth.approval import (
    approve_user,
    ban_user,
    can_login,
    deny_user,
    list_pending,
    register_user,
    unban_user,
)
from outo_models.auth.passwords import hash_password, verify_password
from outo_models.config import Settings, get_settings
from outo_models.db import (
    Approval,
    AuditLog,
    Base,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import (
    ApprovalRequiredError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from outo_models.utils.time import utcnow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def factory(tmp_data_dir) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test sqlite engine + schema; auto-disposed.

    Mirrors the per-file async engine fixture pattern used by the existing
    model tests (`test_models_user.py`, `test_db_session.py`) so each case
    gets a clean schema and no cross-test FK pollution.
    """
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


@pytest.fixture
def approval_required_settings(settings: Settings) -> Settings:
    """Default `Settings` already has `require_approval=True`; expose a clearer alias."""
    return settings


@pytest.fixture
def open_signup_settings(monkeypatch: pytest.MonkeyPatch, tmp_data_dir) -> Settings:
    """Settings with `require_approval=False` for the auto-approve flow."""
    monkeypatch.setenv("OUTO_REQUIRE_APPROVAL", "false")
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


async def _create_admin(
    session: AsyncSession, *, username: str = "root"
) -> User:
    """Insert and return an admin user usable as a `decided_by` actor."""
    admin = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("admin-password-1234"),
        role="admin",
        status="approved",
        approved_at=utcnow(),
    )
    session.add(admin)
    await session.flush()
    return admin


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------


class TestRegisterUserWithApprovalRequired:
    """`register_user` with `require_approval=True` lands users in the pending queue."""

    async def test_creates_user_with_pending_status_and_approval_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            user = await register_user(
                session,
                username="alice",
                email="alice@example.com",
                password="correct horse battery staple",
                settings=approval_required_settings,
            )
            await session.commit()

        assert user.id is not None
        assert user.username == "alice"
        assert user.email == "alice@example.com"
        assert user.status == "pending"
        assert user.is_active is False
        assert user.approved_at is None
        assert user.approved_by_id is None

        async with factory() as session:
            pending_approvals = (
                await session.execute(
                    select(Approval).where(Approval.user_id == user.id)
                )
            ).scalars().all()
            assert len(pending_approvals) == 1
            assert pending_approvals[0].decision == "pending"
            assert pending_approvals[0].decided_at is None

    async def test_password_is_hashed_with_argon2id_not_stored_plain(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            user = await register_user(
                session,
                username="bob",
                email="bob@example.com",
                password="correct horse battery staple",
                settings=approval_required_settings,
            )
            await session.commit()

        assert user.password_hash.startswith("$argon2id$")
        assert "correct horse battery staple" not in user.password_hash
        assert verify_password(user.password_hash, "correct horse battery staple")

    async def test_writes_signup_audit_log_with_actor_id_none(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            user = await register_user(
                session,
                username="carol",
                email="carol@example.com",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            await session.commit()

        async with factory() as session:
            log = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "user.signup")
                    .where(AuditLog.target_type == "user")
                    .where(AuditLog.target_id == str(user.id))
                )
            ).scalar_one()
            assert log.actor_id is None
            assert log.ip is None

    async def test_duplicate_username_raises_conflict_error(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            await register_user(
                session,
                username="dave",
                email="dave@example.com",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await register_user(
                    session,
                    username="dave",
                    email="dave2@example.com",
                    password="hunter22hunter22",
                    settings=approval_required_settings,
                )

    async def test_duplicate_email_raises_conflict_error(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            await register_user(
                session,
                username="erin",
                email="erin@example.com",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await register_user(
                    session,
                    username="erin2",
                    email="erin@example.com",
                    password="hunter22hunter22",
                    settings=approval_required_settings,
                )

    async def test_invalid_username_slug_raises_validation(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            with pytest.raises(Exception) as exc_info:
                await register_user(
                    session,
                    username="Has Spaces",
                    email="spaces@example.com",
                    password="hunter22hunter22",
                    settings=approval_required_settings,
                )
            assert isinstance(exc_info.value, ValidationFailedError)

    async def test_email_is_normalized_to_lowercase(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            user = await register_user(
                session,
                username="frank",
                email="Frank@Example.COM",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            await session.commit()

        assert user.email == "frank@example.com"

    async def test_uniqueness_check_uses_normalized_email(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            await register_user(
                session,
                username="grace",
                email="grace@example.com",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await register_user(
                    session,
                    username="grace2",
                    email="GRACE@example.com",
                    password="hunter22hunter22",
                    settings=approval_required_settings,
                )

    async def test_service_does_not_auto_commit(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            await register_user(
                session,
                username="henry",
                email="henry@example.com",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            # Intentionally do not commit.

        async with factory() as verify_session:
            row = (
                await verify_session.execute(
                    select(User).where(User.username == "henry")
                )
            ).scalar_one_or_none()
            assert row is None


class TestRegisterUserWithApprovalDisabled:
    """`require_approval=False` auto-approves signups and skips the Approval row."""

    async def test_user_is_auto_approved_with_approved_at_and_no_approval_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        open_signup_settings: Settings,
    ) -> None:
        async with factory() as session:
            user = await register_user(
                session,
                username="ivy",
                email="ivy@example.com",
                password="hunter22hunter22",
                settings=open_signup_settings,
            )
            await session.commit()

        assert user.status == "approved"
        assert user.is_active is True
        assert user.approved_at is not None
        # No decision-maker because the operator never clicked approve.
        assert user.approved_by_id is None

        async with factory() as session:
            approvals = (
                await session.execute(
                    select(Approval).where(Approval.user_id == user.id)
                )
            ).scalars().all()
            assert approvals == []

    async def test_can_login_succeeds_when_auto_approved(
        self,
        factory: async_sessionmaker[AsyncSession],
        open_signup_settings: Settings,
    ) -> None:
        async with factory() as session:
            user = await register_user(
                session,
                username="jack",
                email="jack@example.com",
                password="hunter22hunter22",
                settings=open_signup_settings,
            )
            await session.commit()

        # `can_login` is a pure gate against the loaded `User` row —
        # no session needed once the row is in memory.
        assert can_login(user) is None


# ---------------------------------------------------------------------------
# can_login
# ---------------------------------------------------------------------------


class TestCanLogin:
    """`can_login` raises the right typed exception per non-approved status."""

    def test_approved_returns_none(self) -> None:
        user = User(username="k", email="k@example.com", password_hash="h", status="approved")
        assert can_login(user) is None

    def test_pending_raises_approval_required(self) -> None:
        user = User(username="k", email="k@example.com", password_hash="h", status="pending")
        with pytest.raises(ApprovalRequiredError):
            can_login(user)

    def test_denied_raises_forbidden(self) -> None:
        user = User(username="k", email="k@example.com", password_hash="h", status="denied")
        with pytest.raises(ForbiddenError):
            can_login(user)

    def test_banned_raises_forbidden(self) -> None:
        user = User(username="k", email="k@example.com", password_hash="h", status="banned")
        with pytest.raises(ForbiddenError):
            can_login(user)


# ---------------------------------------------------------------------------
# approve_user
# ---------------------------------------------------------------------------


class TestApproveUser:
    """`approve_user` flips pending → approved with full audit bookkeeping."""

    async def test_flips_status_and_sets_approver_metadata(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            pending = User(
                username="larry",
                email="larry@example.com",
                password_hash=hash_password("hunter22hunter22"),
                status="pending",
            )
            session.add(pending)
            approval = Approval(user_id=0, decision="pending")  # placeholder, set below
            session.add(approval)
            await session.flush()
            approval.user_id = pending.id
            await session.commit()

        async with factory() as session:
            approved = await approve_user(session, username="larry", admin=admin)
            await session.commit()

        assert approved.status == "approved"
        assert approved.is_active is True
        assert approved.approved_at is not None
        assert approved.approved_by_id == admin.id

        async with factory() as session:
            approval_row = (
                await session.execute(
                    select(Approval).where(Approval.user_id == approved.id)
                )
            ).scalar_one()
            assert approval_row.decision == "approved"
            assert approval_row.decided_by_id == admin.id
            assert approval_row.decided_at is not None

    async def test_writes_approve_audit_log(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            pending = User(
                username="mike",
                email="mike@example.com",
                password_hash="h",
                status="pending",
            )
            session.add(pending)
            await session.commit()

        async with factory() as session:
            approved = await approve_user(session, username="mike", admin=admin)
            await session.commit()

        async with factory() as session:
            log = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "user.approve")
                    .where(AuditLog.target_id == str(approved.id))
                )
            ).scalar_one()
            assert log.actor_id == admin.id

    async def test_unknown_user_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            await session.commit()

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await approve_user(session, username="ghost", admin=admin)

    async def test_non_pending_user_raises_conflict(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            approved_user = User(
                username="nina",
                email="nina@example.com",
                password_hash="h",
                status="approved",
                approved_at=utcnow(),
            )
            session.add(approved_user)
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await approve_user(session, username="nina", admin=admin)


# ---------------------------------------------------------------------------
# deny_user
# ---------------------------------------------------------------------------


class TestDenyUser:
    """`deny_user` mirrors `approve_user` but lands in the denied state."""

    async def test_flips_status_and_records_reason(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            pending = User(
                username="olive",
                email="olive@example.com",
                password_hash="h",
                status="pending",
            )
            session.add(pending)
            approval = Approval(user_id=0, decision="pending")
            session.add(approval)
            await session.flush()
            approval.user_id = pending.id
            await session.commit()

        async with factory() as session:
            denied = await deny_user(
                session, username="olive", admin=admin, reason="not a fit"
            )
            await session.commit()

        assert denied.status == "denied"
        assert denied.is_active is False

        async with factory() as session:
            approval_row = (
                await session.execute(
                    select(Approval).where(Approval.user_id == denied.id)
                )
            ).scalar_one()
            assert approval_row.decision == "denied"
            assert approval_row.decided_by_id == admin.id
            assert approval_row.reason == "not a fit"
            assert approval_row.decided_at is not None

    async def test_writes_deny_audit_log(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            pending = User(
                username="pearl",
                email="pearl@example.com",
                password_hash="h",
                status="pending",
            )
            session.add(pending)
            await session.commit()

        async with factory() as session:
            denied = await deny_user(session, username="pearl", admin=admin)
            await session.commit()

        async with factory() as session:
            log = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "user.deny")
                    .where(AuditLog.target_id == str(denied.id))
                )
            ).scalar_one()
            assert log.actor_id == admin.id

    async def test_unknown_user_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            await session.commit()

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await deny_user(session, username="ghost", admin=admin)

    async def test_non_pending_user_raises_conflict(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            approved = User(
                username="quinn",
                email="quinn@example.com",
                password_hash="h",
                status="approved",
            )
            session.add(approved)
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await deny_user(session, username="quinn", admin=admin)


# ---------------------------------------------------------------------------
# ban_user
# ---------------------------------------------------------------------------


class TestBanUser:
    """`ban_user` works from any non-banned state but refuses self / admin targets."""

    @pytest.mark.parametrize("starting_status", ["pending", "approved", "denied"])
    async def test_transitions_to_banned_from_any_state(
        self, factory: async_sessionmaker[AsyncSession], starting_status: str
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            target = User(
                username=f"target-{starting_status}",
                email=f"target-{starting_status}@example.com",
                password_hash="h",
                status=starting_status,
            )
            session.add(target)
            await session.commit()

        async with factory() as session:
            banned = await ban_user(
                session, username=f"target-{starting_status}", admin=admin, reason="abuse"
            )
            await session.commit()

        assert banned.status == "banned"

        async with factory() as session:
            log = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "user.ban")
                    .where(AuditLog.target_id == str(banned.id))
                )
            ).scalar_one()
            assert log.actor_id == admin.id

    async def test_already_banned_raises_conflict(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            target = User(
                username="repeat",
                email="repeat@example.com",
                password_hash="h",
                status="banned",
            )
            session.add(target)
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await ban_user(session, username="repeat", admin=admin)

    async def test_self_ban_is_forbidden(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session, username="self")
            await session.commit()

        async with factory() as session:
            with pytest.raises(ForbiddenError):
                await ban_user(session, username="self", admin=admin)

    async def test_banning_another_admin_is_forbidden(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session, username="alpha")
            other_admin = User(
                username="bravo",
                email="bravo@example.com",
                password_hash="h",
                role="admin",
                status="approved",
            )
            session.add(other_admin)
            await session.commit()

        async with factory() as session:
            with pytest.raises(ForbiddenError):
                await ban_user(session, username="bravo", admin=admin)

    async def test_unknown_user_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            await session.commit()

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await ban_user(session, username="ghost", admin=admin)


# ---------------------------------------------------------------------------
# unban_user
# ---------------------------------------------------------------------------


class TestUnbanUser:
    """`unban_user` is the only state transition that goes banned → approved."""

    async def test_flips_banned_to_approved_and_writes_audit(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            target = User(
                username="unban-me",
                email="unban-me@example.com",
                password_hash="h",
                status="banned",
            )
            session.add(target)
            await session.commit()

        async with factory() as session:
            restored = await unban_user(session, username="unban-me", admin=admin)
            await session.commit()

        assert restored.status == "approved"
        assert restored.is_active is True

        async with factory() as session:
            log = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "user.unban")
                    .where(AuditLog.target_id == str(restored.id))
                )
            ).scalar_one()
            assert log.actor_id == admin.id

    async def test_not_banned_raises_conflict(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            target = User(
                username="not-banned",
                email="not-banned@example.com",
                password_hash="h",
                status="approved",
            )
            session.add(target)
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await unban_user(session, username="not-banned", admin=admin)

    async def test_unknown_user_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            admin = await _create_admin(session)
            await session.commit()

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await unban_user(session, username="ghost", admin=admin)


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


class TestListPending:
    """`list_pending` returns only pending users, oldest first."""

    async def test_returns_only_pending_users_in_created_at_order(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Insert three users in deliberate status mix so the filter is exercised.
        async with factory() as session:
            for i, status in enumerate(["pending", "approved", "pending", "denied"]):
                session.add(
                    User(
                        username=f"u{i}",
                        email=f"u{i}@example.com",
                        password_hash="h",
                        status=status,
                    )
                )
            await session.commit()

        async with factory() as session:
            pending = await list_pending(session)

        assert [u.username for u in pending] == ["u0", "u2"]

    async def test_empty_when_no_pending(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            session.add(
                User(
                    username="approved-only",
                    email="approved-only@example.com",
                    password_hash="h",
                    status="approved",
                )
            )
            await session.commit()

        async with factory() as session:
            assert await list_pending(session) == []


# ---------------------------------------------------------------------------
# End-to-end smoke
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """A single user walks through every transition to prove the contract holds."""

    async def test_register_approve_login_then_ban_and_unban(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Register with approval required.
        async with factory() as session:
            await register_user(
                session,
                username="walker",
                email="walker@example.com",
                password="hunter22hunter22",
            )
            await session.commit()

        async with factory() as session:
            refreshed = (
                await session.execute(select(User).where(User.username == "walker"))
            ).scalar_one()
            # Login gate refuses pending users.
            with pytest.raises(ApprovalRequiredError):
                can_login(refreshed)

        # Approve.
        async with factory() as session:
            admin = await _create_admin(session)
            await session.commit()

        async with factory() as session:
            await approve_user(session, username="walker", admin=admin)
            await session.commit()

        async with factory() as session:
            refreshed = (
                await session.execute(select(User).where(User.username == "walker"))
            ).scalar_one()
            assert can_login(refreshed) is None
            assert refreshed.is_active is True

        # Ban.
        async with factory() as session:
            await ban_user(session, username="walker", admin=admin, reason="spam")
            await session.commit()

        async with factory() as session:
            refreshed = (
                await session.execute(select(User).where(User.username == "walker"))
            ).scalar_one()
            with pytest.raises(ForbiddenError):
                can_login(refreshed)

        # Unban.
        async with factory() as session:
            await unban_user(session, username="walker", admin=admin)
            await session.commit()

        async with factory() as session:
            refreshed = (
                await session.execute(select(User).where(User.username == "walker"))
            ).scalar_one()
            assert can_login(refreshed) is None

        # Full audit trail is present in order.
        async with factory() as session:
            actions = (
                await session.execute(
                    select(AuditLog.action)
                    .where(AuditLog.target_id == str(refreshed.id))
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            ).scalars().all()
            assert actions == ["user.signup", "user.approve", "user.ban", "user.unban"]


# ---------------------------------------------------------------------------
# Sanity: existing engine fixtures don't leak IntegrityError on duplicate
# (a robustness check that the service catches conflicts before the DB does).
# ---------------------------------------------------------------------------


class TestConflictRaisedBeforeDbError:
    """`ConflictError` is raised by the service, never an `IntegrityError`."""

    async def test_register_duplicate_username_does_not_leak_integrity_error(
        self,
        factory: async_sessionmaker[AsyncSession],
        approval_required_settings: Settings,
    ) -> None:
        async with factory() as session:
            await register_user(
                session,
                username="zoe",
                email="zoe@example.com",
                password="hunter22hunter22",
                settings=approval_required_settings,
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(ConflictError):
                await register_user(
                    session,
                    username="zoe",
                    email="zoe2@example.com",
                    password="hunter22hunter22",
                    settings=approval_required_settings,
                )
            # Caller never committed the second `add`, so no flush raced to the DB.
            try:
                await session.commit()
            except IntegrityError:  # pragma: no cover - defensive
                pytest.fail("ConflictError must be raised before the IntegrityError")