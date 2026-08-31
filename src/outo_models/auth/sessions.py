"""Signed-cookie session engine for outo-models.

Sessions are a thin layer over `itsdangerous.URLSafeTimedSerializer`: the
secret comes from `Settings.secret_key` (validated to be ≥ 32 chars in
production by `Settings.validate_for_production`), the expiry is a sliding
window managed by the caller, and every wire-format detail of the cookie
flows through `cookie_kwargs` so the security attributes live in exactly
one place.

The serializer is constructed with a domain-local salt so that signing
cookies cannot accidentally be used to forge e.g. password-reset tokens
that share the same secret key.
"""

from __future__ import annotations

from typing import Any, cast

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from outo_models.exceptions import UnauthorizedError

#: Name of the cookie that carries the session payload. Changing this is a
#: breaking change for every browser holding a session — keep it stable.
SESSION_COOKIE_NAME = "outo_session"

#: Salt isolates session signatures from any other itsdangerous use of the
#: same secret key. It is NOT a secret — its purpose is to ensure that a
#: token signed for one purpose cannot be reused as a token for another.
_SESSION_SALT = b"outo-models.session.v1"


class SessionManager:
    """Mint and verify signed-cookie session tokens.

    Construction is cheap (no I/O); the underlying serializer holds the
    secret and salt in memory only.
    """

    def __init__(self, secret_key: str, *, max_age: int) -> None:
        self._max_age = max_age
        self._serializer = URLSafeTimedSerializer(secret_key, salt=_SESSION_SALT)

    @property
    def max_age(self) -> int:
        """Maximum age, in seconds, of a valid session token."""
        return self._max_age

    def dumps(self, data: dict[str, Any]) -> str:
        """Return an opaque, URL-safe signed token containing `data`."""
        return cast(str, self._serializer.dumps(data))

    def loads(self, token: str) -> dict[str, Any]:
        """Return the original `dict` payload iff the token is signed by us and unexpired.

        Raises:
            UnauthorizedError: when the signature is invalid, the token has
                expired, the payload is not a dict, or the token is empty/malformed.
        """
        try:
            data = self._serializer.loads(token, max_age=self._max_age)
        except SignatureExpired as exc:
            raise UnauthorizedError("Session expired") from exc
        except BadSignature as exc:
            raise UnauthorizedError("Invalid session token") from exc
        if not isinstance(data, dict):
            # The serializer happily deserializes any JSON value — only
            # dicts match the SessionManager contract.
            raise UnauthorizedError("Invalid session payload")
        return data


def cookie_kwargs(secure: bool) -> dict[str, Any]:
    """Return the standard `Set-Cookie` security attributes for session cookies.

    `Secure` is bound to the `secure` argument so callers in development
    (HTTP-only) can opt out explicitly; in production, `Settings.base_url`
    determines whether to pass `True`. `HttpOnly`, `SameSite=Lax`, and
    `Path=/` are non-negotiable: `HttpOnly` blocks JS access (XSS hardening),
    `Lax` allows top-level GET navigations (so OIDC-style redirects survive),
    and `Path=/` makes the cookie available to every route.
    """
    return {
        "secure": secure,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
    }
