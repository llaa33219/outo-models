"""Integration tests for the FastAPI app factory + lifespan.

Verifies that `create_app(settings)`:

    * Builds an OpenAPI schema with the expected title / version.
    * Wires every JSON + UI router into the route table.
    * Mounts the git smart-HTTP service last, so the ASGI route list
      still surfaces Starlette's mount entry alongside the HTTP routers.
    * Returns a fresh app per call (no shared state across invocations).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from outo_models import version
from outo_models.server import create_app


class TestCreateApp:
    """`create_app` returns a FastAPI instance with the right metadata."""

    def test_title_and_version(self, app: tuple[TestClient, FastAPI, object]) -> None:
        _, fastapi_app, _ = app
        assert fastapi_app.title == "outo-models"
        assert fastapi_app.version == version.__version__

    def test_returns_fresh_app_each_call(self, tmp_data_dir: Path) -> None:
        a = create_app()
        b = create_app()
        assert a is not b
        assert a.routes is not b.routes

    def test_routes_include_json_and_ui(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        # Each path is checked via HTTP so we exercise the actual router
        # resolution (some route metadata is only visible after the
        # included-router lazy expansion that a request triggers).
        expected = {
            ("/api/auth/signup", "POST", 422),  # 422 because body is missing
            ("/api/auth/login", "POST", 422),
            ("/api/auth/me", "GET", 401),
            ("/api/repos", "GET", 200),
            ("/api/spaces", "GET", 200),
            ("/api/webhooks/test", "POST", 200),
            ("/api/auth/logout", "POST", 200),
            ("/", "GET", 200),
            ("/signup", "GET", 200),
            ("/login", "GET", 200),
        }
        for path, method, expected_status in expected:
            response = client.request(method, path)
            assert response.status_code == expected_status, (
                f"{method} {path} returned {response.status_code}, expected {expected_status}"
            )

    def test_security_headers_present_on_root(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert response.headers["permissions-policy"].startswith("camera=()")
        assert response.headers["content-security-policy"].startswith("default-src")
        # `localhost` is a hostname → internal-mode flag is False → HSTS
        # is emitted. The old "loopback allow-list" special-case is gone.
        assert response.headers.get("strict-transport-security") == (
            "max-age=31536000; includeSubDomains"
        )


class TestErrorEnvelope:
    """Generic exceptions render as the documented JSON envelope, no leaks."""

    def test_validation_error_returns_422_envelope(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        # Short password triggers pydantic validation: min_length=8.
        response = client.post(
            "/api/auth/signup",
            json={"username": "x", "email": "x@example.com", "password": "short"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_failed"
        assert "message" in body


class TestGitServiceMounted:
    """`GET /{owner}/{name}.git/info/refs` is served by the git smart-HTTP app.

    This is the most important full-stack assertion: the git service is
    mounted at root (not under `/git`), and a public repo created via the
    JSON API must be reachable through the same Starlette app the JSON
    routers live in.
    """

    async def test_info_refs_public_repo(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="git-owner")
        client.post(
            "/api/auth/login",
            json={
                "username": "git-owner",
                "password": "correct horse battery staple",
            },
        )
        create_response = client.post(
            "/api/repos",
            json={
                "name": "git-public",
                "kind": "model",
                "visibility": "public",
            },
        )
        assert create_response.status_code == 201
        client.post("/api/auth/logout")

        # Anonymous `info/refs` advertisement must succeed for public repos.
        response = client.get(
            "/git-owner/git-public.git/info/refs",
            params={"service": "git-upload-pack"},
        )
        assert response.status_code == 200, response.text
        # Git's smart-HTTP response includes the service announcement.
        assert b"git-upload-pack" in response.content
