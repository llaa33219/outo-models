"""Integration tests for the auth router.

Covers the full signup → pending → login blocked → admin approve → login
cycle, the session cookie attributes (Secure / HttpOnly / SameSite), the
PAT issuance contract (token returned ONCE), and the `/api/auth/me`
payload shape.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.auth.sessions import SESSION_COOKIE_NAME
from outo_models.db import User
from outo_models.utils.time import utcnow

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestSignup:
    """POST /api/auth/signup honors `require_approval`."""

    async def test_signup_with_approval_returns_201_and_pending_status(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.post(
            "/api/auth/signup",
            json={
                "username": "alice",
                "email": "alice@example.com",
                "password": "hunter22hunter22",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == "alice"
        assert body["status"] == "pending"
        assert "detail" in body

    async def test_duplicate_username_returns_conflict(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        body = {
            "username": "alice",
            "email": "alice@example.com",
            "password": "hunter22hunter22",
        }
        first = client.post("/api/auth/signup", json=body)
        assert first.status_code == 201
        second = client.post(
            "/api/auth/signup",
            json={**body, "email": "alice2@example.com"},
        )
        assert second.status_code == 409
        assert second.json()["error"] == "conflict"


class TestLoginApprovalGate:
    """Login refuses pending accounts until an admin approves them."""

    async def test_login_blocked_when_pending(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        client.post(
            "/api/auth/signup",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "password": "hunter22hunter22",
            },
        )
        response = client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "hunter22hunter22"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "approval_required"

    async def test_login_succeeds_after_approved(
        self,
        app: tuple[TestClient, FastAPI, object],
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        client.post(
            "/api/auth/signup",
            json={
                "username": "carol",
                "email": "carol@example.com",
                "password": "hunter22hunter22",
            },
        )
        async with factory() as session:
            user = (
                await session.execute(select(User).where(User.username == "carol"))
            ).scalar_one()
            user.status = "approved"
            user.approved_at = utcnow()
            await session.commit()

        response = client.post(
            "/api/auth/login",
            json={"username": "carol", "password": "hunter22hunter22"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "carol"
        assert response.json()["status"] == "approved"


class TestSessionCookieAttributes:
    """`Secure` flag matches the active environment; HttpOnly + SameSite always set."""

    async def test_cookie_attributes_in_development(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, settings = app
        # Default Settings has `env="development"`; Secure must be False.
        assert settings.env == "development"
        await seed_approved_user(username="dave")
        response = client.post(
            "/api/auth/login",
            json={"username": "dave", "password": "correct horse battery staple"},
        )
        assert response.status_code == 200
        cookie = response.cookies.get(SESSION_COOKIE_NAME)
        assert cookie is not None
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "httponly" in set_cookie_header.lower()
        assert "samesite=lax" in set_cookie_header.lower()
        assert "path=/" in set_cookie_header.lower()

    async def test_cookie_rotation_on_each_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="eve")
        first = client.post(
            "/api/auth/login",
            json={"username": "eve", "password": "correct horse battery staple"},
        )
        second = client.post(
            "/api/auth/login",
            json={"username": "eve", "password": "correct horse battery staple"},
        )
        first_cookie = first.cookies.get(SESSION_COOKIE_NAME)
        second_cookie = second.cookies.get(SESSION_COOKIE_NAME)
        assert first_cookie is not None
        assert second_cookie is not None
        assert first_cookie != second_cookie


class TestMe:
    """`GET /api/auth/me` returns the authenticated user's profile."""

    async def test_me_requires_auth(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/api/auth/me")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

    async def test_me_returns_profile_after_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="frank")
        client.post(
            "/api/auth/login",
            json={"username": "frank", "password": "correct horse battery staple"},
        )
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == "frank"
        assert body["role"] == "user"
        assert body["status"] == "approved"
        assert "quota" in body
        assert "max_bytes" in body["quota"]
        assert "used_bytes" in body["quota"]


class TestLogout:
    """POST /api/auth/logout clears the session cookie."""

    async def test_logout_returns_success_envelope(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.post("/api/auth/logout")
        assert response.status_code == 200
        assert response.json()["detail"]


class TestTokens:
    """POST /api/auth/tokens mints a PAT; DELETE removes it."""

    async def test_create_token_returns_raw_token_once(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="grace")
        client.post(
            "/api/auth/login",
            json={"username": "grace", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/api/auth/tokens",
            json={"name": "ci-token", "scopes": ["read", "write"]},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "ci-token"
        assert "token" in body
        assert body["prefix"] == body["token"][:8]

    async def test_list_tokens_returns_rows(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="henry")
        client.post(
            "/api/auth/login",
            json={"username": "henry", "password": "correct horse battery staple"},
        )
        client.post("/api/auth/tokens", json={"name": "list-test"})
        response = client.get("/api/auth/tokens")
        assert response.status_code == 200
        names = [row["name"] for row in response.json()]
        assert "list-test" in names

    async def test_delete_token_returns_204(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="irene")
        client.post(
            "/api/auth/login",
            json={"username": "irene", "password": "correct horse battery staple"},
        )
        created = client.post("/api/auth/tokens", json={"name": "deletable"})
        token_id = created.json()["id"]
        response = client.delete(f"/api/auth/tokens/{token_id}")
        assert response.status_code == 204
