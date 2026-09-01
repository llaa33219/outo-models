"""Auth router — signup, login, logout, me, and PAT management.

Rate limiting is applied per-route via `slowapi`'s `@limiter.limit` so
operators can tune the policy from one place. The limiter reads the
remote address out of the request — anonymous endpoints bucket by IP,
so a brute-force attacker pays per source rather than per account.

# allow: SIZE_OK — the WP-13 contract bundles signup + login + logout +
# me + 4 PAT endpoints into a single auth router; splitting would force
# the test suite to track two import paths for the same logical group.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.auth import (
    LOGIN_LIMIT,
    SIGNUP_LIMIT,
    can_login,
    fingerprint,
    limiter,
    register_user,
    verify_password,
)
from outo_models.auth.sessions import SESSION_COOKIE_NAME
from outo_models.auth.tokens import DEFAULT_TOKEN_TTL_SECONDS, TokenService
from outo_models.config import Settings, get_settings
from outo_models.db import PersonalAccessToken, User
from outo_models.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from outo_models.server.deps import get_current_user, get_db
from outo_models.server.routers._auth_helpers import (
    build_session_cookie,
    session_max_age,
    user_dict,
)
from outo_models.utils.slug import validate_slug

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    """POST /api/auth/signup body."""

    username: str = Field(min_length=1, max_length=63)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=512)


class LoginRequest(BaseModel):
    """POST /api/auth/login body."""

    username: str = Field(min_length=1, max_length=63)
    password: str = Field(min_length=1, max_length=512)


class TokenCreateRequest(BaseModel):
    """POST /api/auth/tokens body — mint a new PAT."""

    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    ttl_days: int | None = Field(default=None, ge=1, le=365 * 5)


class TokenRow(BaseModel):
    """PAT metadata returned by GET /api/auth/tokens."""

    id: int
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    created_at: datetime


class TokenCreateResponse(BaseModel):
    """Mint-time response; the raw token is shown ONCE."""

    id: int
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    token: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/signup", status_code=status.HTTP_201_CREATED)
@limiter.limit(SIGNUP_LIMIT)
async def signup(
    request: Request,
    body: SignupRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Register a new account; honors `settings.require_approval`."""
    user = await register_user(
        db,
        username=body.username,
        email=body.email,
        password=body.password,
        settings=settings,
    )
    await db.commit()
    payload: dict[str, object] = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "status": user.status,
    }
    if user.status == "pending":
        payload["detail"] = "Account is pending approval — an administrator must approve it."
    else:
        payload["detail"] = "Account created."
    return JSONResponse(status_code=201, content=payload)


@router.post("/login")
@limiter.limit(LOGIN_LIMIT)
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Verify password + approval state, then rotate the session cookie."""
    username = validate_slug(body.username)
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, body.password):
        # Same error either way so an attacker cannot enumerate usernames.
        raise UnauthorizedError("Invalid username or password")

    can_login(user)
    build_session_cookie(response=response, settings=settings, user_id=user.id)
    return await user_dict(db, user)


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    """Clear the session cookie. Idempotent — no-op on a logged-out client."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return {"detail": "Logged out."}


@router.get("/me")
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """Return the current user's profile + quota + usage."""
    return await user_dict(db, user)


# ---------------------------------------------------------------------------
# PAT management
# ---------------------------------------------------------------------------


def _parse_scopes(raw: str) -> list[str]:
    """Decode the JSON-encoded scopes column. Tolerates malformed rows."""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [s for s in decoded if isinstance(s, str)]


@router.get("/tokens")
async def list_tokens(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TokenRow]:
    """List every PAT owned by the current user."""
    rows = (
        (
            await db.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        TokenRow(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            scopes=_parse_scopes(row.scopes),
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(
    body: TokenCreateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenCreateResponse:
    """Mint a new PAT. The plaintext is returned ONCE in the response."""
    service = TokenService.from_secret(settings.secret_key or "outo-dev-secret")
    ttl_seconds = body.ttl_days * 86400 if body.ttl_days is not None else DEFAULT_TOKEN_TTL_SECONDS
    raw_token = service.issue(
        subject=str(user.id),
        scopes=list(body.scopes),
        ttl_seconds=ttl_seconds,
    )
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
    row = PersonalAccessToken(
        user_id=user.id,
        name=body.name,
        fingerprint_hash=fingerprint(raw_token),
        prefix=raw_token[:8],
        scopes=json.dumps(list(body.scopes)),
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return TokenCreateResponse(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(body.scopes),
        expires_at=row.expires_at,
        token=raw_token,
    )


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    token_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Delete a PAT owned by the current user, or any PAT if admin."""
    row = (
        await db.execute(select(PersonalAccessToken).where(PersonalAccessToken.id == token_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"token {token_id} not found")
    if row.user_id != user.id and user.role != "admin":
        raise ForbiddenError("Cannot delete another user's token")
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router", "session_max_age"]
