"""Integration tests for the spaces router (CRUD + runtime stub)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestCreateSpace:
    """`POST /api/spaces` validates the SDK and writes a sidecar."""

    async def test_create_with_supported_sdk_succeeds(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/api/spaces",
            json={"name": "demo", "sdk": "gradio", "visibility": "public"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "demo"
        assert body["sdk"] == "gradio"
        assert body["visibility"] == "public"

    async def test_create_with_unsupported_sdk_4xx(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/api/spaces",
            json={"name": "demo", "sdk": "huggingface"},
        )
        # Either 404 (NotFoundError) or 422 (validation) is acceptable;
        # the contract is "not 201".
        assert response.status_code in (404, 422)


class TestGetSpace:
    """`GET /api/spaces/{owner}/{name}` includes the runtime block."""

    async def test_runtime_block_reports_failed_state_when_podman_unreachable(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        # v0.5.6: the runtime defaults ON; when the Podman socket is not
        # reachable (the dev / CI environment), the dispatcher falls
        # back to `failed` with the operator hint in `message`. The
        # previous "disabled" state only fires when an operator sets
        # `OUTO_SPACES_RUNTIME_ENABLED=false` explicitly.
        client, _, _ = app
        await seed_approved_user(username="carol")
        client.post(
            "/api/auth/login",
            json={"username": "carol", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "showcase", "sdk": "static", "visibility": "public"},
        )
        response = client.get("/api/spaces/carol/showcase")
        assert response.status_code == 200
        runtime = response.json()["runtime"]
        # No Podman socket mounted in CI → dispatcher reports `failed`
        # rather than 500; the message hints at the runtime manager.
        assert runtime["state"] == "failed"
        assert runtime["url"] is None


class TestListSpaces:
    """`GET /api/spaces` respects visibility filters."""

    async def test_anonymous_sees_only_public(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="dave")
        client.post(
            "/api/auth/login",
            json={"username": "dave", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "public-space", "sdk": "static", "visibility": "public"},
        )
        client.post(
            "/api/spaces",
            json={"name": "private-space", "sdk": "static", "visibility": "private"},
        )
        client.post("/api/auth/logout")
        response = client.get("/api/spaces")
        assert response.status_code == 200
        names = [row["name"] for row in response.json()]
        assert "public-space" in names
        assert "private-space" not in names
