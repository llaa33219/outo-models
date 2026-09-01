"""FastAPI dependencies shared across the router tree.

`get_db` provides the per-request `AsyncSession`; `get_current_user_optional`
and `get_current_user` resolve the request principal through a session
cookie OR a `Authorization: Bearer <PAT>` header. `require_admin` is the
authorization gate every admin endpoint stacks on top of `get_current_user`.

Authentication order is intentional: the session cookie is checked FIRST
because UI flows always carry one, and only then the PAT — so a request
that sends both prefers the cookie (and the cookie's short max-age) over
the long-lived PAT.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.auth.approval import can_login
from outo_models.auth.sessions import SESSION_COOKIE_NAME, SessionManager
from outo_models.auth.tokens import match_fingerprint
from outo_models.config import Settings
from outo_models.db import PersonalAccessToken, User, get_session_factory
from outo_models.exceptions import ForbiddenError, UnauthorizedError

# Session lifetime: 7 days. Kept conservative so a stolen cookie has a
# hard expiry; rotations on login (see auth router) reset the clock.
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an `AsyncSession` scoped to the request; commit/rollback handled.

    The async session factory is built per-engine (see `db.session`) and
    `expire_on_commit=False` is the default so attribute access after
    commit still returns the loaded values without an extra round-trip.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _session_manager(settings: Settings) -> SessionManager:
    """Build a `SessionManager` whose secret comes from `Settings.secret_key`.

    Tests override `Settings.secret_key` to a deterministic string; in
    development the empty default would let anyone forge cookies — that
    is acceptable because nothing is exposed on the public network during
    a local run, and `Settings.validate_for_production` enforces a real
    secret in production.
    """
    return SessionManager(settings.secret_key, max_age=_SESSION_MAX_AGE_SECONDS)


def _settings_for_request(request: Request) -> Settings:
    """Return the active Settings for `request` (or the process-wide default).

    Resolved per-request via the app state populated by `create_app` —
    so dependency wiring does not depend on a frozen module-level value
    and tests that swap `Settings` mid-suite see the new one.
    """
    state_settings = getattr(request.app.state, "settings", None)
    if isinstance(state_settings, Settings):
        return state_settings
    # Fallback: build the global once. Tests wire their own settings via
    # `create_app(settings=...)`, which sets `app.state.settings` and makes
    # this fallback dead code under the standard harness.
    from outo_models.config import get_settings

    return get_settings()


def _resolve_session_user(request: Request, settings: Settings) -> int | None:
    """Return the user id encoded in the session cookie, or `None`.

    A bad signature / expired cookie surfaces as `None` so the caller can
    decide whether to try the bearer-PAT path next. Raising
    `UnauthorizedError` here would force `get_current_user_optional` to
    emit 401 for the wrong reason — an expired cookie is normal lifecycle,
    not a failure.
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    try:
        payload = _session_manager(settings).loads(raw)
    except UnauthorizedError:
        return None
    user_id = payload.get("user_id")
    return int(user_id) if isinstance(user_id, int) else None


async def _resolve_pat_user(db: AsyncSession, *, bearer: str) -> int | None:
    """Return the user id owning the bearer PAT, or `None`.

    The PAT may belong to any user; we cannot know without iterating the
    token fingerprints. argon2 verification is constant-time per row and
    fast enough to run against a handful of PATs — the cardinality of
    PATs per user is small in practice. On a real-world token-store this
    would be backed by a hash lookup, but for v1 a per-user linear scan is
    adequate and keeps the schema free of secrets-on-disk beyond the
    fingerprint.
    """
    candidates = (
        (
            await db.execute(
                select(PersonalAccessToken).where(
                    PersonalAccessToken.expires_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    for pat in candidates:
        if pat.is_expired:
            continue
        if match_fingerprint(pat.fingerprint_hash, bearer):
            return int(pat.user_id)
    return None


async def get_current_user_optional(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """Resolve the request's principal, if any, and cache it for downstream deps.

    Tries the session cookie first, then the bearer PAT. Sets
    `request.state.user_id` so rate-limit keying (`key_by_user_or_ip`)
    can bucket per user without re-doing the lookup.
    """
    settings = _settings_for_request(request)
    user_id = _resolve_session_user(request, settings)
    if user_id is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            user_id = await _resolve_pat_user(db, bearer=token)

    request.state.user_id = user_id
    if user_id is None:
        return None

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        # Treat deleted or deactivated accounts as anonymous — the cookie
        # may outlive the row, and surfacing a 403 on every request would
        # be a worse UX than letting the request continue as anonymous.
        request.state.user_id = None
        return None
    return user


async def get_current_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Require authentication; raise 401 via `UnauthorizedError` when anonymous."""
    if user is None:
        raise UnauthorizedError("Authentication required")
    # `can_login` is the same gate the password-login path uses; calling
    # it here keeps the inactive-account semantics consistent across the
    # browser flow and any PAT-driven flow.
    can_login(user)
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require the current user to be an admin (role == "admin")."""
    if user.role != "admin":
        raise ForbiddenError("Admin role required")
    return user


__all__ = [
    "get_current_user",
    "get_current_user_optional",
    "get_db",
    "require_admin",
]
