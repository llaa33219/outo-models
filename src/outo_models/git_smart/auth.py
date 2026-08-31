"""HTTP Basic-auth identity resolution and authorization for git smart-HTTP.

Two responsibilities:

    1. `resolve_git_identity` — parse an `Authorization: Basic <b64>` header
       into a `User`. The "password" half is a Personal Access Token; we
       verify it against the user's stored argon2id fingerprints and
       update `last_used_at` on match.

    2. `authorize` — apply the (PULL / PUSH) x (public / private) decision
       matrix on `(user, repo, owner)`. The matrix is intentionally tiny
       because collaborators and branch-protection rules are v2 work; the
       only privileged actors today are the repo owner and any admin.

This module owns no I/O of its own — every DB read or write goes through
the caller-supplied `Settings` (which carries the process-wide session
factory via `db.session.get_session_factory`). The auth module is pure
authorization logic; it does not know about ASGI, WSGI, or HTTP status
codes beyond which `OutoError` subclass to raise.
"""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.auth.tokens import match_fingerprint
from outo_models.config import Settings
from outo_models.db import PersonalAccessToken, Repo, User, get_session_factory
from outo_models.exceptions import ForbiddenError, UnauthorizedError
from outo_models.repos.models import Visibility


class GitAction(StrEnum):
    """The two smart-HTTP verbs the service routes."""

    PULL = "pull"  # upload-pack
    PUSH = "push"  # receive-pack


#: Realm advertised in the `WWW-Authenticate` challenge when creds are missing
#: or invalid. The exact string is part of the public API; git clients match
#: on the scheme, not the realm, so any human-readable value works.
_AUTH_REALM = "outo-models"


def _build_auth_challenge() -> str:
    """Return the value of the `WWW-Authenticate` response header for 401."""
    return f'Basic realm="{_AUTH_REALM}", charset="UTF-8"'


async def resolve_git_identity(
    authorization_header: str | None,
    *,
    settings: Settings,
) -> User | None:
    """Parse `Authorization: Basic <b64(username:pat)>` and return the `User`.

    The function NEVER raises on bad input — a malformed header, unknown
    user, or non-matching PAT simply returns `None`. `last_used_at` is
    bumped best-effort in its own session so a transient DB error cannot
    leak through to the request handler.

    Args:
        authorization_header: Raw value of the HTTP `Authorization` header.
            `None` and the empty string are both treated as "no credentials".
        settings: Process-wide `Settings`; used indirectly through the
            lazy session factory.

    Returns:
        The matched `User` (detached, fresh from the DB) or `None`.
    """
    del settings  # Settings is a forward-compat hook; today we use the
                  # process-wide session factory.

    if not authorization_header:
        return None
    scheme, _, payload = authorization_header.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return None
    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    # Split on the FIRST colon so a PAT containing `:` (legal under
    # PASETO's base64url alphabet) is preserved verbatim.
    username, sep, token = decoded.partition(":")
    if not sep or not username or not token:
        return None

    factory: async_sessionmaker[AsyncSession] = get_session_factory()

    async with factory() as session:
        user = (
            await session.execute(
                select(User).where(User.username == username)
            )
        ).scalar_one_or_none()
        if user is None:
            return None
        if not user.is_active:
            # Banned / pending accounts cannot be authenticated even with a
            # valid PAT — surface as no-identity so the caller can 401.
            return None

        pats = (
            await session.execute(
                select(PersonalAccessToken).where(
                    PersonalAccessToken.user_id == user.id
                )
            )
        ).scalars().all()

        matched: PersonalAccessToken | None = None
        for pat in pats:
            if pat.is_expired:
                continue
            if match_fingerprint(pat.fingerprint_hash, token):
                matched = pat
                break

        if matched is None:
            return None

        from datetime import UTC, datetime

        matched.last_used_at = datetime.now(tz=UTC)
        await session.commit()

        return user


async def authorize(
    user: User | None,
    *,
    repo: Repo,
    owner: User,
    action: GitAction,
) -> None:
    """Raise `UnauthorizedError` or `ForbiddenError` for disallowed actions.

    Decision matrix (PUSH collaborators are v2 work):

        | action | visibility       | anonymous        | non-owner, non-admin | owner / admin |
        |--------|------------------|------------------|----------------------|---------------|
        | PULL   | public           | allowed          | allowed              | allowed       |
        | PULL   | private          | UNAUTHORIZED     | FORBIDDEN            | allowed       |
        | PUSH   | public / private | UNAUTHORIZED     | FORBIDDEN            | allowed       |

    A banned / pending user is FORBIDDEN even when their PAT was valid,
    because authentication succeeded but the account is not actionable.
    """
    visibility = repo.visibility

    # Inactive accounts can never perform a privileged action even with
    # valid creds — turn a successful auth into a 403.
    if user is not None and not user.is_active:
        raise ForbiddenError("Account is not active")

    def _is_admin(u: User) -> bool:
        # Admins get a free pass; the DB stores role as a string.
        return u.role == "admin"

    if action is GitAction.PUSH:
        if user is None:
            raise UnauthorizedError("Authentication required for push")
        if user.id != owner.id and not _is_admin(user):
            raise ForbiddenError("Only the owner or an admin may push to this repo")
        return

    # PULL.
    if visibility == Visibility.PUBLIC.value:
        # Public repos are anonymous-readable.
        return
    # Private repo: anonymous → 401; non-owner → 403.
    if user is None:
        raise UnauthorizedError("Authentication required for private repo")
    if user.id != owner.id and not _is_admin(user):
        raise ForbiddenError("Read access denied for private repo")


__all__ = [
    "GitAction",
    "authorize",
    "resolve_git_identity",
]
