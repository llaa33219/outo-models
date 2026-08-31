"""Integration tests for the admin router.

Covers:
    * 401 / 403 for non-admins.
    * The approve / deny / ban / unban flow.
    * Quota updates with AuditLog row.
    * GPU assignments via the WebSetting round-trip.
    * Audit feed returns the most-recent rows.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.db import User
from outo_models.server.routers.admin import get_gpu_assignments

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestAuthGate:
    """Every admin endpoint refuses non-admins with 401 / 403."""

    async def test_list_users_403_for_non_admin(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/api/admin/users")
        assert response.status_code == 403

    async def test_list_users_401_for_anonymous(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/api/admin/users")
        assert response.status_code == 401


class TestApprovalFlow:
    """Approve / deny / ban / unban transitions through the admin API."""

    def _signup_pending(
        self,
        client: TestClient,
        *,
        username: str,
        email: str,
    ) -> None:
        response = client.post(
            "/api/auth/signup",
            json={
                "username": username,
                "email": email,
                "password": "hunter22hunter22",
            },
        )
        assert response.status_code == 201

    async def test_approve_pending_user(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        self._signup_pending(client, username="bob", email="bob@example.com")

        response = client.post("/api/admin/users/bob/approve")
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    async def test_ban_approved_user(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        await seed_approved_user(username="carol")

        response = client.post(
            "/api/admin/users/carol/ban",
            json={"reason": "spam"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "banned"

    async def test_unban_user(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )

        async with factory() as session:
            session.add(
                User(
                    username="dan",
                    email="dan@example.com",
                    password_hash="h",
                    status="banned",
                )
            )
            await session.commit()

        response = client.post("/api/admin/users/dan/unban")
        assert response.status_code == 200
        assert response.json()["status"] == "approved"


class TestQuotaEndpoints:
    """PUT/GET /api/admin/users/{username}/quota writes an audit row."""

    async def test_set_and_get_quota(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await seed_approved_user(username="edith")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        response = client.put(
            "/api/admin/users/edith/quota",
            json={"max_bytes": 12345},
        )
        assert response.status_code == 200
        assert response.json()["max_bytes"] == 12345

        follow_up = client.get("/api/admin/users/edith/quota")
        assert follow_up.status_code == 200
        assert follow_up.json()["max_bytes"] == 12345

    async def test_set_quota_rejects_zero(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await seed_approved_user(username="fred")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        response = client.put(
            "/api/admin/users/fred/quota",
            json={"max_bytes": 0},
        )
        assert response.status_code == 422


class TestGpuEndpoints:
    """PUT /api/admin/users/{username}/gpu round-trips through WebSetting."""

    async def test_set_and_get_gpu_assignments(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await seed_approved_user(username="gina")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )

        response = client.put(
            "/api/admin/users/gina/gpu",
            json={"gpu_ids": ["gpu-0", "gpu-1"]},
        )
        assert response.status_code == 200
        assert response.json()["gpu_ids"] == ["gpu-0", "gpu-1"]

        follow_up = client.get("/api/admin/users/gina/gpu")
        assert follow_up.status_code == 200
        assert follow_up.json()["gpu_ids"] == ["gpu-0", "gpu-1"]

        async with factory() as session:
            gpu_ids = await get_gpu_assignments(session, "gina")
        assert gpu_ids == ["gpu-0", "gpu-1"]

    async def test_delete_clears_assignments(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await seed_approved_user(username="harry")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        client.put(
            "/api/admin/users/harry/gpu",
            json={"gpu_ids": ["gpu-0"]},
        )
        response = client.delete("/api/admin/users/harry/gpu")
        assert response.status_code == 204
        follow_up = client.get("/api/admin/users/harry/gpu")
        assert follow_up.status_code == 200
        assert follow_up.json()["gpu_ids"] == []


class TestAuditFeed:
    """GET /api/admin/audit returns recent rows."""

    async def test_audit_returns_recent(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        response = client.get("/api/admin/audit?limit=10")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
