"""`GET /api/auth/me` — verify the bearer token."""

from __future__ import annotations

import httpx

from outo_models_cli.api import WhoAmI, send, unwrap
from outo_models_cli.errors import BadResponseError, map_response_error


def me(client: httpx.Client) -> WhoAmI:
    """Return the authenticated user's identity.

    Raises `AuthInvalidError` if the token is rejected (HTTP 401).
    """
    response = send(client, "GET", "/api/auth/me")
    if response.status_code >= 400:
        raise map_response_error(response)
    payload = unwrap(response)
    username = payload.get("username")
    role = payload.get("role")
    if not isinstance(username, str) or not isinstance(role, str):
        raise BadResponseError("`/api/auth/me` returned an unexpected payload shape.")
    return WhoAmI(username=username, role=role, server=str(client.base_url))


__all__ = ["me"]
