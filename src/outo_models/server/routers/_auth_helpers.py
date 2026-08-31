"""Shared auth helpers (session cookie + me payload).

Split out from `auth.py` to stay under the per-file line budget. Pure
domain helpers — no FastAPI imports — so the router can stay focused on
route signatures and Pydantic schemas.
"""

from __future__ import annotations

import secrets

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.auth import SessionManager, cookie_kwargs
from outo_models.auth.sessions import SESSION_COOKIE_NAME
from outo_models.config import Settings
from outo_models.db import User
from outo_models.repos.quota import ensure_quota_rows

# Session lifetime: 7 days. A fresh value is minted on every login so an
# attacker who captured a pre-login cookie cannot ride the post-login
# session (session-fixation defense via rotation, not via cookie attrs).
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600


def session_max_age() -> int:
    """Public read-only accessor; tests inspect the same value the router uses."""
    return _SESSION_MAX_AGE_SECONDS


def build_session_cookie(
    *, response: Response, settings: Settings, user_id: int
) -> None:
    """Set the session cookie on `response` using the settings-bound kwargs.

    A random nonce is folded into the payload so two consecutive logins
    still produce distinct cookie values (itsdangerous signs the input
    dict deterministically).
    """
    manager = SessionManager(settings.secret_key, max_age=_SESSION_MAX_AGE_SECONDS)
    token = manager.dumps(
        {"user_id": user_id, "nonce": secrets.token_urlsafe(16)}
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        **cookie_kwargs(secure=settings.env == "production"),
    )


async def user_dict(db: AsyncSession, user: User) -> dict[str, object]:
    """Render the standard `GET /me` payload with quota + usage.

    Quota + usage rows are materialized so the JSON shape is stable
    across `POST /api/auth/login` and `GET /api/auth/me`.
    """
    quota, usage = await ensure_quota_rows(db, user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "display_name": user.display_name,
        "quota": {
            "max_bytes": quota.max_bytes,
            "used_bytes": usage.used_bytes,
        },
        "created_at": user.created_at.isoformat(),
    }


__all__ = ["build_session_cookie", "session_max_age", "user_dict"]
