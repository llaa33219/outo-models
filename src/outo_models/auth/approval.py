"""Signup-approval state machine service.

The signup-approval flow lives entirely in this module so that routers, CLIs,
and tests can drive the same transitions without re-implementing the rules.

Transaction ownership
---------------------
**This module never calls `session.commit()` or `session.rollback()`.** Every
function takes the `AsyncSession` as its first argument and mutates state on
that session; the caller is responsible for committing the surrounding
transaction. This is deliberate: the FastAPI router (WP-13) and the CLI
admin path (WP-14) both need to wrap one or more service calls in a single
transaction (e.g. approve + audit in one round-trip), and the service layer
must not pre-commit on them.

State machine
-------------
::

    [signup]
       │
       ▼
     pending ────approve────▶ approved ─┐
       │                                │
       └──deny─────▶ denied             ├──ban──▶ banned ──unban──▶ approved
                                        │
                                        ▼
                                      banned

`register_user` is the only entry point that creates new users; every other
transition is an admin-mediated state change. All transitions write an
`AuditLog` row so the admin queue can reconstruct who did what.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.auth.passwords import hash_password
from outo_models.config import Settings, get_settings
from outo_models.db import Approval, AuditLog, User
from outo_models.exceptions import (
    ApprovalRequiredError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from outo_models.utils.slug import validate_slug
from outo_models.utils.time import utcnow

# Status strings live as module constants so the state machine cannot
# silently drift if a future migration renames a column.
_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"
_STATUS_DENIED = "denied"
_STATUS_BANNED = "banned"

_AUDIT_TARGET_TYPE_USER = "user"


def _audit_log_row(
    *,
    actor_id: int | None,
    action: str,
    target_id: int,
    detail: str | None = None,
) -> AuditLog:
    """Build an `AuditLog` row for a signup-approval transition.

    `target_type` is hard-coded to `"user"` because every action emitted by
    this module targets a single `User` row. `target_id` is the integer PK
    stringified to match the convention used by other modules.
    """
    return AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=_AUDIT_TARGET_TYPE_USER,
        target_id=str(target_id),
        detail=detail,
    )


def _normalize_email(email: str) -> str:
    """Return `email` stripped and lowercased.

    Email is treated case-insensitively at the boundary; the canonical form
    is the all-lowercase trimmed string. Storing the raw form would let
    `alice@example.com` and `Alice@example.com` coexist, which breaks the
    "one human, one account" rule that drives `ConflictError` semantics.
    """
    return email.strip().lower()


async def _require_user(session: AsyncSession, *, username: str) -> User:
    """Load a `User` by `username` or raise `NotFoundError`.

    Centralises the lookup so every transition has identical error semantics
    (same `code`, same `status_code` mapping) without scattering
    `select(User).where(...)` calls.
    """
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"user {username!r} does not exist")
    return user


async def register_user(
    session: AsyncSession,
    *,
    username: str,
    email: str,
    password: str,
    settings: Settings | None = None,
) -> User:
    """Create a new `User` row in the appropriate initial state.

    Validates the slug policy on `username`, normalizes `email` to lowercase,
    rejects duplicate usernames/emails with `ConflictError`, and writes the
    `User` row plus an `AuditLog(action="user.signup")` entry on the supplied
    session. **Does not commit** — the caller owns the transaction.

    When `settings.require_approval` is true (the production default), the
    new user starts in `"pending"` and gets a paired `Approval(decision=
    "pending")` row. When it is false, the user is auto-approved with
    `approved_at=utcnow()` and `approved_by_id=None`; no `Approval` row is
    created because there was no decision to record.

    Args:
        session: The async session to mutate. Caller commits.
        username: Desired slug. Validated by `validate_slug`.
        email: User's email address. Lowercased before persistence.
        password: Plain-text password. Hashed with argon2id.
        settings: Runtime settings. `None` uses the process-wide singleton
            via `get_settings()`.

    Returns:
        The freshly-constructed (but not yet committed) `User` row.

    Raises:
        ValidationFailedError: `username` violates the slug policy.
        ConflictError: `username` or `email` is already taken.
    """
    # Settings is resolved once and reused so callers can override it for
    # tests without monkey-patching the global cache.
    if settings is None:
        settings = get_settings()

    validated_username = validate_slug(username)
    normalized_email = _normalize_email(email)

    # Uniqueness check happens before any insert so the conflict surfaces as
    # the typed `ConflictError`, not a raw `IntegrityError` from the DB.
    # `expire_on_commit=False` means a previous-committed `User` row is
    # visible to this SELECT; uncommitted session state is *not* visible, so
    # there is no risk of a false positive from a half-finished transaction.
    existing_username = (
        await session.execute(select(User.id).where(User.username == validated_username))
    ).scalar_one_or_none()
    if existing_username is not None:
        raise ConflictError(f"username {validated_username!r} is already taken")

    existing_email = (
        await session.execute(select(User.id).where(User.email == normalized_email))
    ).scalar_one_or_none()
    if existing_email is not None:
        raise ConflictError(
            f"email {normalized_email!r} is already registered"
        )

    # Password hashing is the most expensive thing in this function, so it
    # runs *after* all validation and uniqueness checks — a malformed slug
    # never pays the argon2 cost.
    password_hash = hash_password(password)

    now = utcnow()
    if settings.require_approval:
        user = User(
            username=validated_username,
            email=normalized_email,
            password_hash=password_hash,
            role="user",
            status=_STATUS_PENDING,
            approved_at=None,
            approved_by_id=None,
        )
    else:
        user = User(
            username=validated_username,
            email=normalized_email,
            password_hash=password_hash,
            role="user",
            status=_STATUS_APPROVED,
            approved_at=now,
            approved_by_id=None,
        )
    session.add(user)
    # Flush so `user.id` is populated; the audit row needs it.
    await session.flush()

    if settings.require_approval:
        session.add(Approval(user_id=user.id, decision=_STATUS_PENDING))

    session.add(
        _audit_log_row(
            actor_id=None,
            action="user.signup",
            target_id=user.id,
        )
    )
    return user


async def approve_user(
    session: AsyncSession,
    *,
    username: str,
    admin: User,
) -> User:
    """Transition a pending user to `approved`.

    Sets `User.approved_at` and `User.approved_by_id`, mirrors the decision
    on the paired `Approval` row, and writes `AuditLog(action="user.approve")`.
    **Does not commit** — the caller owns the transaction.

    Raises:
        NotFoundError: `username` does not exist.
        ConflictError: The user is not currently `"pending"`.
    """
    user = await _require_user(session, username=username)
    if user.status != _STATUS_PENDING:
        raise ConflictError(
            f"user {username!r} is not pending (status={user.status!r})"
        )

    now = utcnow()
    user.status = _STATUS_APPROVED
    user.approved_at = now
    user.approved_by_id = admin.id

    approval = await _get_or_create_approval(session, user_id=user.id)
    approval.decision = _STATUS_APPROVED
    approval.decided_by_id = admin.id
    approval.decided_at = now
    approval.reason = None

    session.add(
        _audit_log_row(
            actor_id=admin.id,
            action="user.approve",
            target_id=user.id,
        )
    )
    return user


async def deny_user(
    session: AsyncSession,
    *,
    username: str,
    admin: User,
    reason: str | None = None,
) -> User:
    """Transition a pending user to `denied`.

    Mirrors `approve_user` but records the operator's reason on the
    `Approval` row so the next admin can audit *why* the signup was rejected.
    Writes `AuditLog(action="user.deny")`. **Does not commit**.

    Raises:
        NotFoundError: `username` does not exist.
        ConflictError: The user is not currently `"pending"`.
    """
    user = await _require_user(session, username=username)
    if user.status != _STATUS_PENDING:
        raise ConflictError(
            f"user {username!r} is not pending (status={user.status!r})"
        )

    now = utcnow()
    user.status = _STATUS_DENIED
    user.approved_at = None
    user.approved_by_id = None

    approval = await _get_or_create_approval(session, user_id=user.id)
    approval.decision = _STATUS_DENIED
    approval.decided_by_id = admin.id
    approval.decided_at = now
    approval.reason = reason

    session.add(
        _audit_log_row(
            actor_id=admin.id,
            action="user.deny",
            target_id=user.id,
            detail=reason,
        )
    )
    return user


async def ban_user(
    session: AsyncSession,
    *,
    username: str,
    admin: User,
    reason: str | None = None,
) -> User:
    """Transition any non-banned user to `banned`.

    Banning is the only transition that admits multiple source states: an
    admin can ban a pending signup, an approved user, or a previously denied
    user. Already-banned users raise `ConflictError`. **Does not commit**.

    Safety rails:
        - Self-ban is rejected (`ForbiddenError`). An operator should lock
          themselves out by going through the recovery flow, not via this API.
        - Banning another admin is rejected (`ForbiddenError`). Admins are
          demoted through a separate, audited flow (out of scope for WP-3).

    Raises:
        NotFoundError: `username` does not exist.
        ForbiddenError: `admin.username == username` or the target is an admin.
        ConflictError: The user is already `"banned"`.
    """
    user = await _require_user(session, username=username)

    # Self-ban / admin-ban checks run *before* the status check so that the
    # operator gets the most informative error. A self-ban attempt on a
    # banned admin would otherwise raise `ConflictError`, hiding the real bug.
    if user.username == admin.username:
        raise ForbiddenError("admins cannot ban their own account")
    if user.role == "admin":
        raise ForbiddenError("cannot ban another admin via this endpoint")

    if user.status == _STATUS_BANNED:
        raise ConflictError(f"user {username!r} is already banned")

    user.status = _STATUS_BANNED
    user.approved_at = None
    user.approved_by_id = None

    session.add(
        _audit_log_row(
            actor_id=admin.id,
            action="user.ban",
            target_id=user.id,
            detail=reason,
        )
    )
    return user


async def unban_user(
    session: AsyncSession, *, username: str, admin: User
) -> User:
    """Transition a banned user to `approved`.

    The only legal exit from `"banned"` — the `Approval` row is left as-is
    (it records the historical deny decision if any) so audit history is
    preserved. Writes `AuditLog(action="user.unban")`. **Does not commit**.

    Raises:
        NotFoundError: `username` does not exist.
        ConflictError: The user is not currently `"banned"`.
    """
    user = await _require_user(session, username=username)
    if user.status != _STATUS_BANNED:
        raise ConflictError(
            f"user {username!r} is not banned (status={user.status!r})"
        )

    now = utcnow()
    user.status = _STATUS_APPROVED
    user.approved_at = now
    user.approved_by_id = admin.id

    session.add(
        _audit_log_row(
            actor_id=admin.id,
            action="user.unban",
            target_id=user.id,
        )
    )
    return user


async def list_pending(session: AsyncSession) -> list[User]:
    """Return every pending user, ordered by `created_at` (oldest first).

    The admin queue wants the most-aged signup at the top so it is triaged
    first; ties are broken by `id` to keep the ordering deterministic when
    multiple signups arrive in the same transaction (sqlite stamps the same
    microsecond, making `created_at` alone non-deterministic).
    """
    result = await session.execute(
        select(User)
        .where(User.status == _STATUS_PENDING)
        .order_by(User.created_at, User.id)
    )
    return list(result.scalars().all())


def can_login(user: User) -> None:
    """Raise if `user.status` does not permit authentication; return None otherwise.

    WP-13's login endpoint calls this *after* the password has been verified,
    so a wrong password never discloses the account's approval status —
    only correct passwords reveal it.

    Raises:
        ApprovalRequiredError: `user.status == "pending"`.
        ForbiddenError: `user.status` is `"denied"` or `"banned"`.
    """
    if user.status == _STATUS_PENDING:
        raise ApprovalRequiredError(
            f"user {user.username!r} is awaiting admin approval"
        )
    if user.status in (_STATUS_DENIED, _STATUS_BANNED):
        raise ForbiddenError(f"user {user.username!r} cannot log in")
    return None


async def _get_or_create_approval(session: AsyncSession, *, user_id: int) -> Approval:
    """Return the `Approval` row for `user_id`, creating one if missing.

    In the normal flow `register_user` creates the row, so this is a
    existence check. The fallback path exists because the DB is the source
    of truth: if a row went missing (manual fix, migration bug, etc.) the
    admin transition still completes rather than crashing with `KeyError`.
    """
    approval = (
        await session.execute(
            select(Approval).where(Approval.user_id == user_id)
        )
    ).scalar_one_or_none()
    if approval is None:
        approval = Approval(user_id=user_id, decision=_STATUS_PENDING)
        session.add(approval)
        await session.flush()
    return approval


__all__ = [
    "approve_user",
    "ban_user",
    "can_login",
    "deny_user",
    "list_pending",
    "register_user",
    "unban_user",
]