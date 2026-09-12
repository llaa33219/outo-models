"""Tests for the users router: profile update + repo color."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient


class TestProfileUpdate:
    """POST /api/users/me/profile + repo color PATCH."""

    async def test_update_profile_round_trip(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="zoe")
        client.post(
            "/api/auth/login",
            json={"username": "zoe", "password": "correct horse battery staple"},
        )
        resp = client.post(
            "/api/users/me/profile",
            json={
                "display_name": "Zoe Zhang",
                "bio": "building tiny models",
                "interests": ["NLP", "Vision"],
                "links": [
                    {"label": "blog", "url": "https://zoe.example.com"},
                    {"label": "github", "url": "https://github.com/zoe"},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        profile = client.get("/api/users/zoe").json()
        assert profile["display_name"] == "Zoe Zhang"
        assert profile["bio"] == "building tiny models"
        assert profile["interests"] == ["nlp", "vision"]
        assert {link["label"] for link in profile["links"]} == {"blog", "github"}

    async def test_profile_validation_rejects_bad_inputs(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="yara")
        client.post(
            "/api/auth/login",
            json={"username": "yara", "password": "correct horse battery staple"},
        )
        resp = client.post(
            "/api/users/me/profile",
            json={"links": [{"label": "x", "url": "ftp://nope.example.com"}]},
        )
        assert resp.status_code == 422, resp.text
        resp = client.post(
            "/api/users/me/profile",
            json={"interests": ["way too long tag name with spaces!!"]},
        )
        assert resp.status_code == 422

    async def test_repo_color_patch_and_clear(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="xen")
        client.post(
            "/api/auth/login",
            json={"username": "xen", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "colored", "kind": "model", "visibility": "public"},
        )
        resp = client.patch("/api/repos/xen/colored", json={"color": "#a4c8ff"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["color"] == "#a4c8ff"
        detail = client.get("/api/repos/xen/colored").json()
        assert detail["color"] == "#a4c8ff"
        # clear it
        resp = client.patch("/api/repos/xen/colored", json={"color": ""})
        assert resp.status_code == 200, resp.text
        assert resp.json()["color"] is None
        # invalid color
        resp = client.patch("/api/repos/xen/colored", json={"color": "not-a-color"})
        assert resp.status_code == 422
