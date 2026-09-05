"""Integration tests for the social-graph REST surface.

Covers:

    * Anonymous → 401 on `POST/DELETE /like`.
    * Authenticated idempotent like/unlike (201 → 200 on repeat).
    * Anonymous like-count read on a public repo.
    * Private-repo social actions return 404 to non-owners.
    * Comments are validated for blank / over-long bodies (422).
    * Self-follow is rejected with 403; follow unknown user is 404.
    * Follow endpoints are idempotent.

The conftest fixtures (`app`, `seed_approved_user`) drive the full
lifespan — migrations + scheduler + git smart-HTTP mount — so this is
the closest the suite gets to a real HTTP request.

# allow: SIZE_OK — the v0.3.0 ownership list specifies a single
# `tests/integration/test_social_api.py` covering likes / comments /
# follows / card / files endpoints.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from dulwich import porcelain
from fastapi import FastAPI
from fastapi.testclient import TestClient

from outo_models.repos.storage import repo_fs_path

# `app` and `seed_approved_user` come from tests/integration/conftest.py.


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


class TestLike:
    async def test_anonymous_like_returns_401(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/repos",
            json={"name": "m1", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.post("/api/repos/alice/m1/like")
        assert response.status_code == 401

    async def test_authenticated_like_then_unlike_idempotent(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "m2", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        await seed_approved_user(username="bob")
        _login(client, "bob")

        first = client.post("/api/repos/alice/m2/like")
        assert first.status_code == 201
        body = first.json()
        assert body["liked"] is True
        assert body["like_count"] == 1

        second = client.post("/api/repos/alice/m2/like")
        assert second.status_code == 200
        assert second.json()["like_count"] == 1

        delete_first = client.delete("/api/repos/alice/m2/like")
        assert delete_first.status_code == 204

        delete_second = client.delete("/api/repos/alice/m2/like")
        assert delete_second.status_code == 204

    async def test_private_repo_like_returns_404_for_stranger(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "secret", "kind": "model", "visibility": "private"},
        )
        client.post("/api/auth/logout")
        await seed_approved_user(username="mallory")
        _login(client, "mallory")
        response = client.post("/api/repos/alice/secret/like")
        assert response.status_code == 404

    async def test_owner_can_like_own_private_repo(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "owner-secret", "kind": "model", "visibility": "private"},
        )
        response = client.post("/api/repos/alice/owner-secret/like")
        assert response.status_code == 201

    async def test_get_like_state_for_anon_returns_false(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "anon-like-state", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.get("/api/repos/alice/anon-like-state/like")
        assert response.status_code == 200
        body = response.json()
        assert body["liked"] is False
        assert body["like_count"] == 0


class TestComments:
    async def test_post_then_list_returns_payload(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "comments-repo", "kind": "model", "visibility": "public"},
        )

        posted = client.post(
            "/api/repos/alice/comments-repo/comments",
            json={"body": "first comment"},
        )
        assert posted.status_code == 201
        created = posted.json()
        assert created["author"] == "alice"
        assert created["body"] == "first comment"
        assert "created_at" in created

        listed = client.get("/api/repos/alice/comments-repo/comments")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["author"] == "alice"
        assert rows[0]["body"] == "first comment"

    async def test_blank_body_returns_422(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "c2", "kind": "model", "visibility": "public"},
        )
        response = client.post(
            "/api/repos/alice/c2/comments",
            json={"body": "   "},
        )
        assert response.status_code == 422

    async def test_too_long_body_returns_422(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "c3", "kind": "model", "visibility": "public"},
        )
        response = client.post(
            "/api/repos/alice/c3/comments",
            json={"body": "x" * 4001},
        )
        assert response.status_code == 422


class TestFollow:
    async def test_follow_and_unfollow_idempotent(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")

        _login(client, "alice")
        first = client.post("/api/users/bob/follow")
        assert first.status_code == 201
        assert first.json()["following"] is True
        assert first.json()["follower_count"] == 1

        second = client.post("/api/users/bob/follow")
        assert second.status_code == 200
        assert second.json()["following"] is True

        delete = client.delete("/api/users/bob/follow")
        assert delete.status_code == 204

        delete_again = client.delete("/api/users/bob/follow")
        assert delete_again.status_code == 204

    async def test_self_follow_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.post("/api/users/alice/follow")
        assert response.status_code == 403

    async def test_follow_unknown_user_returns_404(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.post("/api/users/ghost/follow")
        assert response.status_code == 404


class TestCard:
    async def test_card_endpoint_returns_structured_payload(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "carded", "kind": "model", "visibility": "public"},
        )
        response = client.get("/api/repos/alice/carded/card")
        assert response.status_code == 200
        body = response.json()
        assert "front_matter" in body
        assert "body_html" in body
        assert "tags" in body


class TestFiles:
    async def test_files_endpoint_lists_root(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "filed", "kind": "model", "visibility": "public"},
        )
        work = tmp_data_dir / "src-filed"
        work.mkdir()
        (work / "hello.txt").write_text("hi")
        porcelain.init(str(work))
        porcelain.add(str(work), paths=["hello.txt"])
        porcelain.commit(
            str(work),
            message=b"init",
            author=b"a <a@a>",
            committer=b"a <a@a>",
        )
        bare = repo_fs_path("alice", "filed")
        bare.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(bare)
        porcelain.clone(str(work), str(bare), bare=True)

        response = client.get("/api/repos/alice/filed/files")
        assert response.status_code == 200
        body = response.json()
        assert body["path"] == ""
        names = [entry["name"] for entry in body["entries"]]
        assert "hello.txt" in names

    async def test_files_empty_repo_returns_404(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "filed-empty", "kind": "model", "visibility": "public"},
        )
        response = client.get("/api/repos/alice/filed-empty/files")
        assert response.status_code == 404

    async def test_files_traversal_returns_404(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "filed2", "kind": "model", "visibility": "public"},
        )
        response = client.get("/api/repos/alice/filed2/files", params={"path": "../etc"})
        assert response.status_code == 404
