"""HTTP client construction helpers.

Centralises the timeout / header / base-URL defaults so every API call
shares the same transport. The factory returns a fresh `httpx.Client`
each call — short-lived, single-purpose clients are easier to reason
about than a long-lived shared one (no state bleed between commands),
and the cost is one TCP handshake per CLI invocation.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

# 30s connect / read budget — the server is local in a self-hosted
# deployment, but a slow disk or large multipart may legitimately hold
# the connection for a few seconds. Anything longer is genuinely hung.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# Long operations need long read timeouts. A multipart commit after a
# multi-hundred-GiB LFS session does real server-side work (worktree
# clone, fetch, quota, audit) that can exceed 30 s — the field failure
# surfaced as "Cannot reach the server" while the server was alive and
# working. Writes stay unlimited: the transport buffers, so a slow disk
# on either side must not abort a 3 GiB PUT mid-stream.
TIMEOUT_BATCH = httpx.Timeout(120.0, connect=5.0)
TIMEOUT_COMMIT = httpx.Timeout(600.0, connect=5.0)
TIMEOUT_PUT = httpx.Timeout(None, connect=10.0, read=900.0)


def build_client(
    base_url: str,
    token: str,
    *,
    follow_redirects: bool = False,
    **client_kwargs: Any,
) -> httpx.Client:
    """Return an `httpx.Client` pre-wired with the bearer credential.

    Args:
        base_url: The full origin the server listens on. The path prefix
            (`/api`, `/{owner}/{name}/resolve/...`) is appended per call.
        token: Personal Access Token used as the bearer credential.
        follow_redirects: Set to `True` for LFS pointer redirects. Other
            endpoints MUST stay non-redirecting so a misconfigured proxy
            cannot silently absorb a token via 30x.
        **client_kwargs: Forwarded to `httpx.Client` (used by tests to
            inject a `respx` transport via `transport=`).
    """
    headers = {"Authorization": f"Bearer {token}"}
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_TIMEOUT,
        headers=headers,
        follow_redirects=follow_redirects,
        **client_kwargs,
    )


def build_async_client(
    base_url: str,
    token: str,
    *,
    follow_redirects: bool = False,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Async equivalent of `build_client`.

    Used by the parallel `download` command. `base_url` is normalized the
    same way as the sync version so URL joins are consistent.
    """
    headers = {"Authorization": f"Bearer {token}"}
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=_TIMEOUT,
        headers=headers,
        follow_redirects=follow_redirects,
        **client_kwargs,
    )


def basic_auth_header(username: str, token: str) -> str:
    """Build the `Authorization` value for HTTP Basic auth.

    The server's LFS and `/resolve/...` endpoints expect Basic auth,
    not Bearer; this helper produces the canonical `Basic <b64(user:pat)>`
    string. The username is required so a missing username surfaces
    here (typed error) rather than as a server-side 401.
    """
    if not username:
        raise ValueError("Basic auth requires a username; resolve it via /api/auth/me first.")
    raw = f"{username}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def build_basic_client(
    base_url: str,
    username: str,
    token: str,
    *,
    follow_redirects: bool = False,
    **client_kwargs: Any,
) -> httpx.Client:
    """Synchronous client pre-wired with HTTP Basic auth.

    Used by the LFS upload path (`POST /info/lfs/objects/batch` and the
    subsequent PUTs). `follow_redirects` defaults to False because the
    batch endpoint never redirects; callers that need to follow the
    `/resolve/...` 302 to `/info/lfs/objects/{oid}` should pass True.
    """
    headers = {"Authorization": basic_auth_header(username, token)}
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_TIMEOUT,
        headers=headers,
        follow_redirects=follow_redirects,
        **client_kwargs,
    )


def build_basic_async_client(
    base_url: str,
    username: str,
    token: str,
    *,
    follow_redirects: bool = True,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Async Basic-auth client with redirect-following enabled by default.

    The parallel download command uses this client so the `GET
    /{owner}/{name}/resolve/{revision}/{path}` 302 to
    `/info/lfs/objects/{oid}` is followed transparently. httpx forwards
    the `Authorization` header on same-origin redirects (the redirect
    target is always same-origin here), so the LFS GET handler receives
    the same Basic credential the resolve request used.
    """
    headers = {"Authorization": basic_auth_header(username, token)}
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=_TIMEOUT,
        headers=headers,
        follow_redirects=follow_redirects,
        **client_kwargs,
    )


__all__ = [
    "TIMEOUT_BATCH",
    "TIMEOUT_COMMIT",
    "TIMEOUT_PUT",
    "basic_auth_header",
    "build_async_client",
    "build_basic_async_client",
    "build_basic_client",
    "build_client",
]
