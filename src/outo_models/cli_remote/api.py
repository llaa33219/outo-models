"""Admin REST client — `outo-models admin --remote ...` talks to a running
server via the `/api/admin/*` endpoints over HTTPS.

The client is a thin wrapper around `httpx.Client` carrying a bearer PAT
(`Authorization: Bearer <token>`). Each admin command builds a short-lived
client, calls exactly one endpoint, and lets the response drive its
Korean CLI output.

Why a dedicated class rather than a bag of `httpx.post(...)` calls?
    * Centralises error mapping — every network failure becomes the same
      Korean CLI message the operator sees, never a traceback.
    * Centralises the base-URL contract — `--remote` rewrites the host,
      `--api-url` lets ops target a non-default port, and the rest of the
      admin code path never sees the difference.
    * Centralises teardown — the `__exit__` path closes the underlying
      client so a CLI command can't leak a connection on Ctrl-C.

The class does NOT parse JSON responses — it returns the raw `dict` / `list`
and lets the caller decide how to format it for the operator. That keeps
this file small enough to audit at a glance (the security boundary is the
PAT, not the JSON shape).
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from outo_models.exceptions import OutoError

# Default port the bundled Caddy container fronts — HTTPS terminates at
# Caddy, so the admin client always speaks 443 unless `--api-url` says
# otherwise. Tests point this at an ephemeral port via `transport=`.
_DEFAULT_BASE_URL = "https://localhost"

# 30s connect / read budget — the server is local, but `git clone` and
# similar long operations can keep the connection busy; a small budget
# catches genuine outages without false-positiving on slow queries.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class AdminApiError(OutoError):
    """A typed error raised by `AdminApiClient` for every transport failure.

    `code` is one of:
        * `admin_unreachable` — connection refused, DNS failure, timeout.
        * `admin_auth_failed` — server returned 401 / 403.
        * `admin_bad_response` — non-JSON body, unexpected status code.

    The Korean message is operator-facing and deliberately free of bearer
    tokens / host names that could leak into shell history.
    """


class AdminApiClient:
    """Synchronous client over `/api/admin/*`.

    Args:
        base_url: The full origin the server listens on (e.g.
            `https://models.example.com`). Path prefix `/api/admin` is
            appended automatically.
        token: Personal Access Token used as the bearer credential.

    The caller is expected to use the client as a context manager so the
    underlying `httpx.Client` is closed even on `SystemExit`:
    ::

        with AdminApiClient(base_url, token) as api:
            api.approve("alice")

    A bare instantiation is also allowed — the client is cheap to discard,
    and the GC will eventually close the underlying socket. Tests rely on
    the explicit `with` form for determinism.
    """

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        # `transport=` lets tests inject `respx` / `httpx.MockTransport`
        # without monkeypatching — important because the admin tests are
        # marked as `asyncio_mode=auto` integration tests and need a
        # deterministic wire.
        self._owns_client = transport is None
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=_TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the underlying `httpx.Client`. Idempotent."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AdminApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def list_users(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        """Return every user, optionally filtered by `status_filter`."""
        params: dict[str, str] = {}
        if status_filter:
            params["status"] = status_filter
        result = self._request_json("GET", "/users", params=params)
        return cast(list[dict[str, Any]], result)

    def approve(self, username: str) -> dict[str, Any]:
        """POST /api/admin/users/{username}/approve."""
        return cast(dict[str, Any], self._request_json("POST", f"/users/{username}/approve"))

    def deny(self, username: str, *, reason: str | None = None) -> dict[str, Any]:
        """POST /api/admin/users/{username}/deny (optional `reason`)."""
        return cast(
            dict[str, Any],
            self._request_json("POST", f"/users/{username}/deny", json={"reason": reason}),
        )

    def ban(self, username: str, *, reason: str | None = None) -> dict[str, Any]:
        """POST /api/admin/users/{username}/ban (optional `reason`)."""
        return cast(
            dict[str, Any],
            self._request_json("POST", f"/users/{username}/ban", json={"reason": reason}),
        )

    def unban(self, username: str) -> dict[str, Any]:
        """POST /api/admin/users/{username}/unban."""
        return cast(dict[str, Any], self._request_json("POST", f"/users/{username}/unban"))

    def set_quota(self, username: str, max_bytes: int) -> dict[str, Any]:
        """PUT /api/admin/users/{username}/quota."""
        return cast(
            dict[str, Any],
            self._request_json("PUT", f"/users/{username}/quota", json={"max_bytes": max_bytes}),
        )

    def get_quota(self, username: str) -> dict[str, Any]:
        """GET /api/admin/users/{username}/quota."""
        return cast(dict[str, Any], self._request_json("GET", f"/users/{username}/quota"))

    def set_gpu(self, username: str, gpu_ids: list[str]) -> dict[str, Any]:
        """PUT /api/admin/users/{username}/gpu."""
        return cast(
            dict[str, Any],
            self._request_json("PUT", f"/users/{username}/gpu", json={"gpu_ids": list(gpu_ids)}),
        )

    def clear_gpu(self, username: str) -> None:
        """DELETE /api/admin/users/{username}/gpu (idempotent).

        Returns None on success (the server replies 204 with no body).
        """
        self._request_json("DELETE", f"/users/{username}/gpu")

    # ------------------------------------------------------------------
    # Internal: HTTP wrapper with single error funnel
    # ------------------------------------------------------------------

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Issue `method /api/admin{path}` and return the parsed JSON body.

        Raises:
            AdminApiError: wraps every transport failure with a stable
                `code` (see class docstring) and a Korean message free of
                bearer tokens / host details.
        """
        try:
            response = self._client.request(
                method,
                f"/api/admin{path}",
                json=json,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise AdminApiError(
                "원격 관리 API에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요.",
                code="admin_unreachable",
            ) from exc

        if response.status_code in (401, 403):
            raise AdminApiError(
                "인증에 실패했습니다. PAT가 유효한지 확인해 주세요.",
                code="admin_auth_failed",
            )
        if response.status_code == 204 or not response.content:
            return None
        if response.status_code >= 400:
            raise AdminApiError(
                f"원격 관리 API가 오류를 반환했습니다 (HTTP {response.status_code}).",
                code="admin_bad_response",
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise AdminApiError(
                "원격 관리 API가 잘못된 응답을 반환했습니다.",
                code="admin_bad_response",
            ) from exc
        return payload


__all__ = ["AdminApiClient", "AdminApiError"]
