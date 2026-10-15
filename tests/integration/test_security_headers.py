"""Integration tests for security headers on every response (incl. errors).

The contract: every response — successful page, successful API, 404,
422 validation, 401 auth, 500 generic — carries the security header
bundle. HSTS is omitted in internal / IP mode and emitted for real
hostnames — see `TestHsts` below for the matrix.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

# `app` fixture is auto-discovered from tests/integration/conftest.py.


@dataclass
class _StubSettings:
    """Bare-bones stand-in for `Settings` exposing only `is_internal`.

    `SecurityHeadersMiddleware` reads `_settings.is_internal`. Tests for
    the internal-mode branches build a stub instead of bootstrapping a
    full Settings via `create_app`.
    """

    is_internal: bool


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

    def test_hsts_absent_for_localhost(self, app: tuple[TestClient, FastAPI, object]) -> None:
        # localhost is internal mode (plain HTTP) — HSTS must NOT be emitted;
        # it would force browsers onto an HTTPS endpoint that does not exist.
        client, _, _ = app
        response = client.get("/")
        assert response.headers.get("strict-transport-security") is None


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

    def test_font_src_allows_pretendard_cdn(self, app: tuple[TestClient, FastAPI, object]) -> None:
        # 디자인.md §4.1 ships the Pretendard @font-face from jsdelivr; the
        # browser needs an explicit `font-src` allowlist for the woff2 files
        # to load. Internal installs fall back to system-ui when the CDN is
        # unreachable, but the allowlist itself must be present.
        client, _, _ = app
        response = client.get("/")
        csp = response.headers["content-security-policy"]
        assert "font-src 'self' https://cdn.jsdelivr.net" in csp


class TestHstsByInternalMode:
    """`_should_emit_hsts` follows `Settings.is_internal` directly."""

    def _should_emit(self, *, is_internal: bool) -> bool:
        from outo_models.server.middleware import SecurityHeadersMiddleware

        async def _noop_app(scope, receive, send):
            return None

        mw = SecurityHeadersMiddleware(_noop_app, settings=_StubSettings(is_internal=is_internal))  # type: ignore[arg-type]
        return mw._should_emit_hsts()

    def test_emitted_when_external(self) -> None:
        assert self._should_emit(is_internal=False) is True

    def test_suppressed_when_internal(self) -> None:
        assert self._should_emit(is_internal=True) is False


def _assert_common_headers(headers) -> None:
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["permissions-policy"].startswith("camera=()")
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


class TestSpaceRunFramingHeaders:
    """The Space run proxy is framed by the Space page — SAMEORIGIN only.

    The run path serves user-owned app content (a static export or a
    reverse-proxied container). `SAMEORIGIN` + a framing-only CSP lets the
    preview iframe render while the `script-src` allowlist of the rest of
    the site would block the app's own scripts.
    """

    def _run_headers(self, app: tuple[TestClient, FastAPI, object], path: str):
        client, _, _ = app
        return client.get(path).headers

    def test_run_root_allows_same_origin_framing(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        headers = self._run_headers(app, "/spaces/alice/demo/run/")
        assert headers["x-frame-options"] == "SAMEORIGIN"
        csp = headers["content-security-policy"]
        assert "frame-ancestors 'self'" in csp
        assert "frame-ancestors 'none'" not in csp

    def test_run_path_drops_script_and_default_src(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        headers = self._run_headers(app, "/spaces/alice/demo/run/index.html")
        csp = headers["content-security-policy"]
        assert "script-src" not in csp
        assert "default-src" not in csp
        assert "style-src" not in csp
        assert "img-src" not in csp
        assert "font-src" not in csp

    def test_run_path_keeps_the_other_security_headers(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        headers = self._run_headers(app, "/spaces/alice/demo/run/")
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert headers["permissions-policy"].startswith("camera=()")

    def test_non_run_spaces_page_still_denies_framing(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        headers = self._run_headers(app, "/spaces")
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]

    def test_lookalike_path_without_run_segment_denies_framing(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        headers = self._run_headers(app, "/spaces/alice/runner/index.html")
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]


# Attach the helper as a method on the test classes so the existing
# `self._assert_common_headers(...)` calls in the class bodies resolve.
for _cls in (TestSecurityHeadersOnSuccess, TestSecurityHeadersOnErrors):
    _cls._assert_common_headers = staticmethod(_assert_common_headers)  # type: ignore[attr-defined]
