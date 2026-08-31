"""Rate-limiting primitives for outo-models.

The slowapi `Limiter` is constructed here without a Flask app — the FastAPI
integration layer (in `outo_models.server`) will attach the limiter to the
ASGI app via `Limiter.init_app` (or the FastAPI equivalent) and decorate
routes with `@limiter.limit(<NAME>)`. Keeping the Limiter and its key
functions in this module makes the rate-limiting policy unit-testable
without spinning up the whole app.

Two key functions are exposed:

* `key_by_ip` — bucket per remote address. Used for unauthenticated endpoints
  (login, signup) so a brute-force attacker pays per IP, not per account.
* `key_by_user_or_ip` — bucket per authenticated user, with IP fallback.
  Used for authenticated endpoints (git push, API) so legitimate users
  sharing a NAT are not rate-limited together, while unauthenticated
  requests (e.g. during a flaky session) still get bucketed sensibly.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

#: Limit applied to `/login` — five attempts per minute per IP.
LOGIN_LIMIT = "5/minute"
#: Limit applied to `/signup` — three signups per minute per IP.
SIGNUP_LIMIT = "3/minute"
#: Limit applied to git push endpoints — thirty pushes per minute per user.
GIT_PUSH_LIMIT = "30/minute"
#: Limit applied to git pull endpoints — one hundred and twenty pulls per minute per user.
GIT_PULL_LIMIT = "120/minute"
#: Default limit for the public REST API — two hundred and forty requests per minute per user.
API_LIMIT = "240/minute"


def key_by_ip(request: Request) -> str:
    """Bucket every request by its remote IP address."""
    return get_remote_address(request)


def key_by_user_or_ip(request: Request) -> str:
    """Bucket by authenticated user id, falling back to remote IP.

    The `user:<id>` prefix keeps authenticated and anonymous buckets
    disjoint so a user cannot be "rescued" by switching to anonymous
    traffic and vice versa.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return get_remote_address(request)


#: Process-wide Limiter. The FastAPI integration wires it up to the app at
#: boot; this module exposes it so tests and the server module share a
#: single instance.
limiter = Limiter(key_func=key_by_user_or_ip)
