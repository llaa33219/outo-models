"""Shared UI helpers (CSRF issuance + verification).

Split out from `ui.py` to keep the page-rendering routes lean and to
keep the CSRF serializer / cookie-name constants in one place.

The scheme is the textbook double-submit cookie:

    1. GET forms issue an `_csrf` cookie whose value is an
       `itsdangerous`-signed random token.
    2. The same token is rendered as a hidden `<input name="_csrf">`.
    3. POST handlers call `verify_csrf` to compare the cookie vs the
       form value; mismatch → 403.
"""

from __future__ import annotations

import secrets

from fastapi import Response
from itsdangerous import URLSafeTimedSerializer
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from outo_models.auth import cookie_kwargs
from outo_models.config import Settings, get_settings

CSRF_COOKIE = "_csrf"
CSRF_FIELD = "_csrf"
CSRF_MAX_AGE_SECONDS = 24 * 3600
_CSRF_SALT = b"outo-models.csrf.v1"


def _csrf_serializer(settings: Settings) -> URLSafeTimedSerializer:
    """Tiny serializer over `Settings.secret_key` so CSRF tokens can't be reused as PATs."""
    return URLSafeTimedSerializer(settings.secret_key or "outo-dev-secret", salt=_CSRF_SALT)


def issue_csrf_cookie(*, response: Response, settings: Settings) -> str:
    """Mint a fresh CSRF token, attach it to `response`, and return the raw value."""
    serializer = _csrf_serializer(settings)
    random_bytes = secrets.token_bytes(24)
    token_raw: object = serializer.dumps(random_bytes.hex())
    token = str(token_raw)
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        max_age=CSRF_MAX_AGE_SECONDS,
        **cookie_kwargs(secure=settings.env == "production"),
    )
    return token


def read_csrf_cookie(request: Request, settings: Settings) -> str | None:
    """Return the signed CSRF token from the request cookie, or `None`."""
    raw = request.cookies.get(CSRF_COOKIE)
    return raw if raw else None


def verify_csrf(request: Request, form_token: str | None) -> None:
    """Compare cookie vs form CSRF tokens; raise 403 on mismatch / absence."""
    settings = get_settings()
    cookie_token = read_csrf_cookie(request, settings)
    if not cookie_token or not form_token:
        raise StarletteHTTPException(status_code=403, detail="CSRF token missing")
    if not secrets.compare_digest(str(cookie_token), str(form_token)):
        raise StarletteHTTPException(status_code=403, detail="CSRF token mismatch")


def ensure_csrf(request: Request, response: Response) -> None:
    """Issue a CSRF cookie on first GET so forms always have one to send back."""
    settings = get_settings()
    if request.cookies.get(CSRF_COOKIE):
        return
    issue_csrf_cookie(response=response, settings=settings)


__all__ = [
    "CSRF_COOKIE",
    "CSRF_FIELD",
    "ensure_csrf",
    "issue_csrf_cookie",
    "read_csrf_cookie",
    "verify_csrf",
]
