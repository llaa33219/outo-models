"""Authentication and authorization primitives.

This package owns the password / session / PAT / permission / rate-limit
primitives other modules import. It deliberately contains no FastAPI
routers and no database models — those live elsewhere and depend on
what is re-exported here.

Public API:
    Passwords:
        - `hash_password`, `verify_password`, `needs_rehash`
    Sessions:
        - `SessionManager`, `SESSION_COOKIE_NAME`, `cookie_kwargs`
    Personal Access Tokens:
        - `TokenService`, `TokenClaims`, `DEFAULT_TOKEN_TTL_SECONDS`
        - `fingerprint`, `match_fingerprint`
    Permissions:
        - `Scope`, `ROLE_SCOPES`, `has_scope`
    Rate limiting:
        - `limiter`, `key_by_ip`, `key_by_user_or_ip`
        - `LOGIN_LIMIT`, `SIGNUP_LIMIT`,
          `GIT_PUSH_LIMIT`, `GIT_PULL_LIMIT`, `API_LIMIT`
"""

from outo_models.auth.approval import (
    approve_user,
    ban_user,
    can_login,
    deny_user,
    list_pending,
    register_user,
    unban_user,
)
from outo_models.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from outo_models.auth.permissions import ROLE_SCOPES, Scope, has_scope
from outo_models.auth.rate_limit import (
    API_LIMIT,
    GIT_PULL_LIMIT,
    GIT_PUSH_LIMIT,
    LOGIN_LIMIT,
    SIGNUP_LIMIT,
    key_by_ip,
    key_by_user_or_ip,
    limiter,
)
from outo_models.auth.sessions import (
    SESSION_COOKIE_NAME,
    SessionManager,
    cookie_kwargs,
)
from outo_models.auth.tokens import (
    DEFAULT_TOKEN_TTL_SECONDS,
    TokenClaims,
    TokenService,
    fingerprint,
    match_fingerprint,
)

__all__ = [
    "API_LIMIT",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "GIT_PULL_LIMIT",
    "GIT_PUSH_LIMIT",
    "LOGIN_LIMIT",
    "ROLE_SCOPES",
    "SESSION_COOKIE_NAME",
    "SIGNUP_LIMIT",
    "Scope",
    "SessionManager",
    "TokenClaims",
    "TokenService",
    "approve_user",
    "ban_user",
    "can_login",
    "cookie_kwargs",
    "deny_user",
    "fingerprint",
    "has_scope",
    "hash_password",
    "key_by_ip",
    "key_by_user_or_ip",
    "limiter",
    "list_pending",
    "match_fingerprint",
    "needs_rehash",
    "register_user",
    "unban_user",
    "verify_password",
]
