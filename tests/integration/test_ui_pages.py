"""Integration tests for the UI router (Jinja pages + CSRF).

Covers:
    * `/` renders the repos list page with the CSRF cookie set.
    * `/signup`, `/login` render forms with the `_csrf` hidden input.
    * Form POST without a matching CSRF token → 403.
    * The admin page is gated on `role == "admin"`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Fixtures `app` and `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestPublicPages:
    """Anonymous pages render 200 and set the CSRF cookie."""

    async def test_root_renders_list_page(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        assert "outo-models" in response.text

    async def test_signup_page_sets_csrf_cookie(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/signup")
        assert response.status_code == 200
        assert "_csrf" in response.cookies
        assert 'name="_csrf"' in response.text

    async def test_login_page_sets_csrf_cookie(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/login")
        assert response.status_code == 200
        assert "_csrf" in response.cookies
        assert 'name="_csrf"' in response.text


class TestRepoDetailPage:
    """`/{owner}/{name}` renders a repo page for public repos."""

    async def test_anonymous_can_view_public_repo_page(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "show-me", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.get("/alice/show-me")
        assert response.status_code == 200
        assert "show-me" in response.text

    async def test_unknown_repo_returns_404(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/nobody/nothing")
        assert response.status_code == 404


class TestCsrfProtection:
    """Form POST without a matching CSRF token is rejected with 403."""

    async def test_signup_post_without_csrf_is_rejected(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.post(
            "/signup",
            data={
                "username": "eve",
                "email": "eve@example.com",
                "password": "hunter22hunter22",
            },
        )
        assert response.status_code == 403


class TestAdminPageGate:
    """`/admin` requires the admin role."""

    async def test_anonymous_admin_page_is_401(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/admin")
        assert response.status_code == 401

    async def test_non_admin_admin_page_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/admin")
        assert response.status_code == 403

    async def test_admin_renders_dashboard(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        response = client.get("/admin")
        assert response.status_code == 200
        assert "Admin" in response.text or "admin" in response.text.lower()
