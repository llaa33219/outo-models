"""HTTP client construction helpers.

Centralises the timeout / header / base-URL defaults so every API call
shares the same transport. The factory returns a fresh `httpx.Client`
each call — short-lived, single-purpose clients are easier to reason
about than a long-lived shared one (no state bleed between commands),
and the cost is one TCP handshake per CLI invocation.
"""

from __future__ import annotations

from typing import Any

import httpx

# 30s connect / read budget — the server is local in a self-hosted
# deployment, but a slow disk or large multipart may legitimately hold
# the connection for a few seconds. Anything longer is genuinely hung.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


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


__all__ = ["build_async_client", "build_client"]
