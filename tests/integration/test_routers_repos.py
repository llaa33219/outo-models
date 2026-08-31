"""Integration tests for the repos router (CRUD + visibility rules)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestCreateRepo:
    """`POST /api/repos` requires auth + creates the bare repo on disk."""

    async def test_create_requires_auth(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.post(
            "/api/repos",
            json={"name": "x", "kind": "model", "visibility": "private"},
        )
        assert response.status_code == 401

    async def test_create_returns_clone_url_and_metadata(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/api/repos",
            json={
                "name": "my-model",
                "kind": "model",
                "visibility": "public",
                "description": "hello",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "my-model"
        assert body["kind"] == "model"
        assert body["visibility"] == "public"
        assert body["clone_url"].endswith("/alice/my-model.git")


class TestListRepos:
    """`GET /api/repos` returns public repos to anonymous callers."""

    async def test_list_filters_to_public_for_anonymous(
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
            json={"name": "public-repo", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos",
            json={"name": "secret-repo", "kind": "model", "visibility": "private"},
        )

        client.post("/api/auth/logout")
        response = client.get("/api/repos")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert "public-repo" in names
        assert "secret-repo" not in names


class TestGetRepo:
    """`GET /api/repos/{owner}/{name}` returns metadata + recent revisions."""

    async def test_get_public_repo_for_anonymous(
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
            json={"name": "open-repo", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.get("/api/repos/alice/open-repo")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "open-repo"
        assert body["visibility"] == "public"
        assert "recent_revisions" in body

    async def test_private_repo_404_for_other_user(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "secret", "kind": "model", "visibility": "private"},
        )
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.get("/api/repos/alice/secret")
        assert response.status_code == 404

    async def test_owner_sees_private_repo(
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
            json={"name": "secret", "kind": "model", "visibility": "private"},
        )
        response = client.get("/api/repos/alice/secret")
        assert response.status_code == 200


class TestPatchRepo:
    """`PATCH /api/repos/{owner}/{name}` updates visibility / description."""

    async def test_owner_can_patch_visibility(
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
            json={"name": "to-promote", "kind": "model", "visibility": "private"},
        )
        response = client.patch(
            "/api/repos/alice/to-promote",
            json={"visibility": "public"},
        )
        assert response.status_code == 200
        assert response.json()["visibility"] == "public"

    async def test_non_owner_cannot_patch(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "shared", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.patch(
            "/api/repos/alice/shared",
            json={"description": "stolen"},
        )
        assert response.status_code == 403


class TestDeleteRepo:
    """`DELETE /api/repos/{owner}/{name}` requires owner or admin."""

    async def test_owner_can_delete(
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
            json={"name": "to-delete", "kind": "model", "visibility": "private"},
        )
        response = client.delete("/api/repos/alice/to-delete")
        assert response.status_code == 204
        follow_up = client.get("/api/repos/alice/to-delete")
        assert follow_up.status_code == 404

    async def test_non_owner_cannot_delete(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "mine", "kind": "model", "visibility": "private"},
        )
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.delete("/api/repos/alice/mine")
        assert response.status_code == 403
