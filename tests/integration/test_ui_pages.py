"""Integration tests for the UI router (Jinja pages + CSRF).

Covers:
    * `/` renders the repos list page with the CSRF cookie set.
    * `/signup`, `/login` render forms with the `_csrf` hidden input.
    * Form POST without a matching CSRF token → 403.
    * The admin page is gated on `role == "admin"`.
    * The navbar shows login/signup when anonymous and the profile chip when authed.
    * `/models`, `/datasets`, `/spaces` list public repos of the right kind only.
    * `/{username}` renders a profile page with repos grouped by kind and 404s
      for unknown users.
    * `/new` (GET + POST) is login-gated, creates a repo on success, and
      re-renders with an error on conflict.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Fixtures `app` and `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestPublicPages:
    """Anonymous pages render 200 and set the CSRF cookie."""

    async def test_root_renders_list_page(self, app: tuple[TestClient, FastAPI, object]) -> None:
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

    async def test_unknown_repo_returns_404(self, app: tuple[TestClient, FastAPI, object]) -> None:
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


class TestCsrfTokenConsistency:
    """Field failure: 'CSRF token mismatch' on a normal login — the hidden
    field was rendered from request.cookies while the response cookie was
    minted separately. They must be the SAME token on the SAME response."""

    def test_first_visit_form_token_equals_set_cookie(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        import re

        client, _, _ = app
        response = client.get("/login")
        assert response.status_code == 200
        cookie_token = response.cookies.get("_csrf")
        assert cookie_token
        match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
        assert match is not None
        assert match.group(1) == cookie_token

    def test_reload_reuses_cookie_token(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        first = client.get("/login")
        token = first.cookies.get("_csrf")
        assert token
        second = client.get("/login")  # TestClient's jar replays the cookie
        match = re.search(r'name="_csrf" value="([^"]+)"', second.text)
        assert match is not None
        assert match.group(1) == token


def _form_csrf(client: TestClient, path: str) -> str:
    """GET `path` and return the CSRF field value matching the cookie.

    Some subsequent GETs reuse the same cookie (no Set-Cookie is emitted),
    so the helper falls back to the jar's existing `_csrf` cookie when the
    response did not mint a new one.
    """
    response = client.get(path)
    assert response.status_code == 200, path
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None
    assert match.group(1) == cookie_token, path
    return cookie_token


class TestNavbarContext:
    """The navbar reflects the current viewer's auth state."""

    async def test_anonymous_navbar_shows_login_and_signup(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        # Login / sign-up entry points must be present.
        assert 'href="/login"' in body
        assert 'href="/signup"' in body
        # No profile chip for anonymous — the CSS class name appears in the
        # embedded stylesheet too, so we anchor the check on the actual <a>.
        assert 'class="profile-chip"' not in body
        # Kind links exist regardless of auth state.
        assert 'href="/models"' in body
        assert 'href="/datasets"' in body
        assert 'href="/spaces"' in body

    async def test_authenticated_navbar_shows_profile_chip(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        assert "profile-chip" in body
        assert 'href="/alice"' in body
        # Username shown in the chip — just the initial "A" rendered as the avatar.
        assert 'class="avatar"' in body
        # Login / sign-up get replaced by the chip + New button when authed.
        assert 'class="new-btn"' in body
        assert 'href="/new"' in body
        # The "<username>" appears inside the chip's username span.
        match = re.search(r"profile-chip[^>]*>.*?alice.*?</a>", body, flags=re.DOTALL)
        assert match is not None


class TestKindCatalogPages:
    """`/models`, `/datasets`, `/spaces` filter by kind and visibility."""

    async def _seed(self, app, seed_approved_user) -> TestClient:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "shown-model", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos",
            json={"name": "shown-dataset", "kind": "dataset", "visibility": "public"},
        )
        client.post(
            "/api/repos",
            json={"name": "shown-space", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos",
            json={"name": "hidden-model", "kind": "model", "visibility": "private"},
        )
        # Bob has an additional model that should also be visible.
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "bob-model", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        return client

    async def test_models_lists_only_public_models(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._seed(app, seed_approved_user)
        response = client.get("/models")
        assert response.status_code == 200
        body = response.text
        assert "alice/shown-model" in body
        assert "alice/shown-space" in body  # name collides but kind is model
        assert "bob/bob-model" in body
        assert "alice/shown-dataset" not in body  # wrong kind
        assert "alice/hidden-model" not in body  # private
        # Heading + active-nav.
        assert ">Models<" in body

    async def test_datasets_lists_only_public_datasets(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._seed(app, seed_approved_user)
        response = client.get("/datasets")
        assert response.status_code == 200
        body = response.text
        assert "alice/shown-dataset" in body
        for forbidden in ("shown-model", "shown-space", "bob-model", "hidden-model"):
            assert forbidden not in body, forbidden

    async def test_spaces_lists_only_public_spaces(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        # Seed alice + create one space via the API; no need to pollute with
        # the broader _seed helper that also creates models + datasets.
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        # /api/spaces takes `sdk`, not `kind`. Use the static SDK for a
        # vanilla Space that exercises the v1 metadata layer.
        client.post(
            "/api/spaces",
            json={"name": "shown-space-real", "sdk": "static", "visibility": "public"},
        )
        client.post(
            "/api/spaces",
            json={"name": "hidden-space-real", "sdk": "static", "visibility": "private"},
        )
        client.post("/api/auth/logout")
        response = client.get("/spaces")
        assert response.status_code == 200
        body = response.text
        assert "alice/shown-space-real" in body
        assert "alice/hidden-space-real" not in body  # private
        assert "alice/shown-model" not in body
        assert "alice/shown-dataset" not in body

    async def test_models_empty_state_when_none(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/models")
        assert response.status_code == 200
        body = response.text
        assert "No public models yet" in body
        # Empty state hints at sign-up for anonymous users.
        assert 'href="/signup"' in body

    async def test_models_kind_links_use_active_nav(
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
            json={"name": "m1", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.get("/models")
        body = response.text
        # The Models navbar link is marked active.
        assert 'href="/models" class="nav-link active"' in body
        # Datasets / Spaces navbar links are NOT marked active.
        assert 'href="/datasets" class="nav-link active"' not in body
        assert 'href="/spaces" class="nav-link active"' not in body


class TestProfilePage:
    """`/{username}` shows avatar + repos grouped by kind."""

    async def _seed_repos(self, client: TestClient) -> None:
        client.post(
            "/api/repos",
            json={"name": "alpha", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos",
            json={"name": "beta", "kind": "dataset", "visibility": "public"},
        )
        client.post(
            "/api/repos",
            json={"name": "gamma", "kind": "space", "visibility": "private"},
        )

    async def test_profile_renders_avatar_and_repos_by_kind(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await self._seed_repos(client)
        client.post("/api/auth/logout")

        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        # Avatar + name + repos.
        assert "alice" in body.lower()
        assert ">A<" in body or "avatar" in body
        assert "alice/alpha" in body
        assert "alice/beta" in body
        # Private repos are visible to anonymous? No — only public are listed.
        assert "alice/gamma" not in body
        # Tab labels.
        for label in ("Models", "Datasets", "Spaces"):
            assert label in body

    async def test_profile_renders_all_repos_for_self(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await self._seed_repos(client)
        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        # Self sees private too.
        assert "alice/gamma" in body
        # And the new-repo button is rendered on self.
        assert ">New<" in body

    async def test_profile_renders_all_repos_for_admin(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="root", role="admin")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await self._seed_repos(client)
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        response = client.get("/alice")
        assert response.status_code == 200
        assert "alice/gamma" in response.text

    async def test_unknown_user_returns_404(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/nobody")
        assert response.status_code == 404

    async def test_invalid_username_slug_returns_404(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/Not%20Allowed")
        assert response.status_code == 404


class TestNewRepoPage:
    """`/new` requires login, creates a repo on POST, re-renders on conflict."""

    async def test_new_anonymous_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/new", follow_redirects=False)
        # 303 redirect to /login?next=/new
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert "next=/new" in response.headers["location"]
        # No body for a redirect.
        assert "Create a new repository" not in response.text

    async def test_new_authenticated_renders_form(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/new")
        assert response.status_code == 200
        body = response.text
        for needle in (
            "Create a new repository",
            'name="kind"',
            'name="name"',
            'name="visibility"',
            'name="description"',
            'value="model"',
            'value="dataset"',
            'value="space"',
            "_csrf",
        ):
            assert needle in body, needle

    async def test_new_kind_query_param_pre_selects_kind(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/new?kind=dataset")
        assert response.status_code == 200
        assert 'value="dataset" selected' in response.text

    async def test_new_post_without_csrf_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/new",
            data={
                "kind": "model",
                "name": "no-csrf",
                "visibility": "private",
                "description": "",
            },
        )
        assert response.status_code == 403

    async def test_new_post_creates_model_and_redirects(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/new")
        response = client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "model",
                "name": "fresh-model",
                "visibility": "public",
                "description": "hello",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/alice/fresh-model"
        # GET /alice/fresh-model renders the repo overview.
        repo = client.get("/alice/fresh-model")
        assert repo.status_code == 200
        assert "fresh-model" in repo.text

    async def test_new_post_creates_dataset(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/new")
        response = client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "dataset",
                "name": "fresh-ds",
                "visibility": "private",
                "description": "my data",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/alice/fresh-ds"

    async def test_new_post_creates_space(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/new")
        response = client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "space",
                "name": "fresh-space",
                "visibility": "public",
                "description": "showcase",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/alice/fresh-space"

    async def test_new_post_conflict_rerenders_with_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        # Create once.
        csrf = _form_csrf(client, "/new")
        first = client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "model",
                "name": "clash",
                "visibility": "private",
                "description": "",
            },
            follow_redirects=False,
        )
        assert first.status_code == 303
        # Try again with the SAME name.
        csrf2 = _form_csrf(client, "/new")
        second = client.post(
            "/new",
            data={
                "_csrf": csrf2,
                "kind": "model",
                "name": "clash",
                "visibility": "private",
                "description": "",
            },
            follow_redirects=False,
        )
        assert second.status_code == 200
        body = second.text
        # Conflict surfaces in the in-page error block.
        assert "errors" in body
        assert "already exists" in body
        # Field values are preserved on re-render.
        assert 'value="clash"' in body
        assert ">Create<" in body or "Create" in body

    async def test_new_post_invalid_slug_rerenders_with_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/new")
        response = client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "model",
                "name": "Not Allowed",
                "visibility": "private",
                "description": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "errors" in response.text

    async def test_new_post_bad_kind_rerenders_with_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/new")
        response = client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "truck",
                "name": "ok",
                "visibility": "private",
                "description": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "Unknown repository kind" in response.text


class TestNavbarLeftAlignedAuth:
    """Product spec: auth controls (login/signup, or the profile chip when
    authed) sit on the LEFT side of the navbar — not the HF-default right."""

    def test_login_link_left_of_spacer_anonymous(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        nav = response.text.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        spacer = nav.find("nav-spacer")
        login = nav.find('href="/login"')
        signup = nav.find('href="/signup"')
        models = nav.find('href="/models"')
        assert spacer != -1 and login != -1 and signup != -1 and models != -1
        assert models < login < spacer, "login must be between nav links and the spacer"
        assert models < signup < spacer

    def test_profile_chip_left_of_spacer_authed(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        client.post(
            "/api/auth/signup",
            json={"username": "carol", "email": "carol@example.com", "password": "password123!"},
        )
        # Approval-gated installs: approve via the admin API path or set the
        # account active directly, then log in through the UI session.
        from sqlalchemy import select

        from outo_models.db import get_session_factory
        from outo_models.db.models import User

        async def _approve() -> None:
            factory = get_session_factory()
            async with factory() as session:
                user = (
                    await session.execute(select(User).where(User.username == "carol"))
                ).scalar_one()
                user.status = "approved"
                await session.commit()

        import asyncio

        asyncio.run(_approve())
        client.post("/api/auth/login", json={"username": "carol", "password": "password123!"})
        response = client.get("/")
        nav = response.text.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        spacer = nav.find("nav-spacer")
        chip = nav.find('class="profile-chip"')
        assert chip != -1 and spacer != -1
        assert chip < spacer, "profile chip must be on the left of the spacer"
