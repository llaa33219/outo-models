"""Integration tests for security headers on every response (incl. errors).

The contract: every response — successful page, successful API, 404,
422 validation, 401 auth, 500 generic — carries the security header
bundle. HSTS is omitted on the loopback dev domain.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

# `app` fixture is auto-discovered from tests/integration/conftest.py.


class TestSecurityHeadersOnSuccess:
    """Headers on 2xx responses."""

    def test_headers_on_root(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        self._assert_common_headers(response.headers)

    def test_headers_on_api_webhook(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.post("/api/webhooks/test")
        assert response.status_code == 200
        self._assert_common_headers(response.headers)

    def test_no_hsts_on_localhost(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/")
        assert "strict-transport-security" not in response.headers


class TestSecurityHeadersOnErrors:
    """Headers are emitted on error responses too."""

    def test_headers_on_404(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/no-such-page")
        assert response.status_code == 404
        self._assert_common_headers(response.headers)

    def test_headers_on_422(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.post(
            "/api/auth/signup",
            json={"username": "x", "email": "x@example.com", "password": "short"},
        )
        assert response.status_code == 422
        self._assert_common_headers(response.headers)

    def test_headers_on_401(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/api/auth/me")
        assert response.status_code == 401
        self._assert_common_headers(response.headers)


class TestCspContents:
    """The CSP stays restrictive — no `unsafe-inline` for scripts."""

    def test_script_src_is_self_only(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/")
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        # No inline scripts.
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]

    def test_style_src_allows_inline_for_template(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        csp = response.headers["content-security-policy"]
        assert "style-src 'self' 'unsafe-inline'" in csp


def _assert_common_headers(headers) -> None:
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["permissions-policy"].startswith("camera=()")
    assert headers["content-security-policy"].startswith("default-src 'self'")


# Attach the helper as a method on the test classes so the existing
# `self._assert_common_headers(...)` calls in the class bodies resolve.
for _cls in (TestSecurityHeadersOnSuccess, TestSecurityHeadersOnErrors):
    _cls._assert_common_headers = staticmethod(_assert_common_headers)  # type: ignore[attr-defined]
