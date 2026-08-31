"""Integration tests for the users router (public profile + repos)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.db import User
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestPublicProfile:
    """`GET /api/users/{username}` renders a public profile."""

    async def test_profile_for_existing_user(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        response = client.get("/api/users/alice")
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == "alice"
        assert "display_name" in body
        assert "created_at" in body
        assert "public_repo_count" in body

    async def test_profile_404_for_unknown_user(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/api/users/ghost")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"


class TestUserReposVisibility:
    """`GET /api/users/{username}/repos` respects visibility rules."""

    async def test_anonymous_sees_only_public_repos(
        self,
        app: tuple[TestClient, FastAPI, object],
        factory: async_sessionmaker,
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="bob")
        async with factory() as session:
            owner = (
                await session.execute(select(User).where(User.username == "bob"))
            ).scalar_one()
            await create_repo(
                session,
                owner=owner,
                name="public-repo",
                kind=RepoKind.MODEL,
                visibility=Visibility.PUBLIC,
            )
            await create_repo(
                session,
                owner=owner,
                name="secret-repo",
                kind=RepoKind.MODEL,
                visibility=Visibility.PRIVATE,
            )
            await session.commit()

        response = client.get("/api/users/bob/repos")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert "public-repo" in names
        assert "secret-repo" not in names

    async def test_owner_sees_own_private_repos(
        self,
        app: tuple[TestClient, FastAPI, object],
        factory: async_sessionmaker,
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="carol")
        client.post(
            "/api/auth/login",
            json={"username": "carol", "password": "correct horse battery staple"},
        )

        async with factory() as session:
            owner = (
                await session.execute(select(User).where(User.username == "carol"))
            ).scalar_one()
            await create_repo(
                session,
                owner=owner,
                name="private-repo",
                kind=RepoKind.MODEL,
                visibility=Visibility.PRIVATE,
            )
            await session.commit()

        response = client.get("/api/users/carol/repos")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert "private-repo" in names
