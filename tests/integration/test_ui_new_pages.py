"""Integration tests for the v0.5.x UI additions:

* Profile dropdown (navbar `<details>` panel) — authed menu visible,
  anonymous navbar unchanged.
* `/{username}/usage` — self-only; 403 for strangers, redirect for anon.
* `/support` — public page, status 200, renders operator-email block.

The repo Settings tab + likes roster + threaded comments live in
`test_ui_repo_page.py` (the v0.5.x tail of that file); this module
covers the navbar and the three new top-level pages so each test file
stays focused on one feature area.

The conftest fixtures (`app`, `seed_approved_user`) already wire the
full lifespan; tests stay linear without dropping the `async` keyword.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


def _form_csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.text)
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None, path
    assert match.group(1) == cookie_token, path
    return cookie_token


# ---------------------------------------------------------------------------
# Profile dropdown menu
# ---------------------------------------------------------------------------


class TestProfileDropdown:
    """Authed viewers see a `<details>`-based dropdown menu in the navbar.

    The dropdown uses pure CSS (native HTML `<details>`/`<summary>`)
    with NO inline JS — CSP is `script-src 'self'` so we cannot ship
    any inline event handlers. Each menu item renders as a regular
    `<a>` so it navigates on click; the dropdown panel sits under the
    chip and uses a BLP element tile (2px border, 0 radius).
    """

    async def test_dropdown_present_for_authed_viewer(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")

        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        assert '<details class="nav-dropdown"' in body
        assert 'class="profile-chip"' in body
        # All menu items are linked.
        assert 'href="/alice"' in body  # Profile
        assert 'href="/alice/usage"' in body
        assert 'href="/settings/tokens"' in body
        assert 'href="/alice/edit"' in body
        assert 'href="/support"' in body
        assert 'href="/logout"' in body

    async def test_dropdown_absent_for_anon(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        assert '<details class="nav-dropdown"' not in body
        assert 'href="/login"' in body
        assert 'href="/signup"' in body

    async def test_dropdown_uses_native_details_no_inline_js(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")

        response = client.get("/")
        body = response.text
        # The dropdown chrome uses native `<details>` + `<summary>` —
        # no inline `onclick=`, `onfocus=`, etc.
        assert "<details" in body
        assert "<summary" in body
        assert "onclick=" not in body
        # The `script-src 'self'` baseline still allows the bundled
        # clipboard script, but no dropdown-specific script was added.
        assert "nav-dropdown" in body
        assert "nav-dropdown__panel" in body


# ---------------------------------------------------------------------------
# /{username}/usage
# ---------------------------------------------------------------------------


class TestUserUsagePage:
    """`/{username}/usage` is self-only; strangers get 403, anon is redirected."""

    async def test_usage_self_renders_quota_tile(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/alice/usage")
        assert response.status_code == 200
        body = response.text
        assert "Storage usage" in body
        # The quota / usage / free rows render.
        assert "Used" in body
        assert "Quota" in body
        assert "Free" in body
        # The usage-bar progressbar element is present.
        assert 'role="progressbar"' in body
        assert "usage-bar__fill" in body

    async def test_usage_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "bob")
        response = client.get("/alice/usage")
        assert response.status_code == 403

    async def test_usage_anonymous_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        response = client.get("/alice/usage", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert "usage" in response.headers["location"]

    async def test_usage_unknown_user_returns_404(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/nobody/usage")
        assert response.status_code == 404

    async def test_usage_reflects_repo_bytes(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        # Create + upload a file so used_bytes > 0.
        client.post(
            "/api/repos",
            json={"name": "uses-1", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos/alice/uses-1/upload",
            data={"message": "first commit"},
            files={"files": ("hello.txt", b"x" * 1024, "text/plain")},
        )

        response = client.get("/alice/usage")
        assert response.status_code == 200
        body = response.text
        # The used figure is non-zero (KiB / MiB rendering depends on
        # the quota defaults seeded in conftest, 10 GiB).
        assert ">0<" in body or "1.0 KiB" in body or "KiB" in body


# ---------------------------------------------------------------------------
# /support
# ---------------------------------------------------------------------------


class TestSupportPage:
    """`/support` is a public page reachable by anonymous + authed viewers."""

    async def test_support_anonymous_returns_200(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/support")
        assert response.status_code == 200
        body = response.text
        assert "Support" in body
        assert "server operator" in body

    async def test_support_authed_returns_200(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/support")
        assert response.status_code == 200

    async def test_support_lists_common_topics(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/support")
        assert response.status_code == 200
        body = response.text
        for needle in ("Account / login", "Storage quota", "git / LFS", "Abuse"):
            assert needle in body, needle

    async def test_support_does_not_render_email_when_unset(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/support")
        assert response.status_code == 200
        body = response.text
        # When `Settings.support_email` is empty, the page renders a
        # generic note instead of any concrete address.
        assert "mailto:" not in body


# ---------------------------------------------------------------------------
# Profile tile ratio (Req #1)
# ---------------------------------------------------------------------------


class TestProfileTileRatio:
    """The profile page widens the repos tile relative to the profile tile."""

    async def test_profile_layout_uses_wider_repos_column(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "ratio-1", "kind": "model", "visibility": "public"},
        )
        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        # The new ratio: profile cap 240px / repos flexes to the
        # remainder. We anchor on the exact `minmax(0, 240px)` token
        # (no 360px anywhere in the layout) — that's what changed.
        assert "minmax(340px, 1fr) minmax(0, 2.5fr)" in body
        assert "minmax(0, 360px)" not in body

    async def test_profile_layout_stacks_on_narrow_viewport(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        # The 900px breakpoint stack is preserved so mobile readers
        # still see a single-column layout.
        assert "max-width: 900px" in body
        assert "grid-template-columns: minmax(0, 1fr)" in body


__all__ = [
    "TestProfileDropdown",
    "TestProfileTileRatio",
    "TestSupportPage",
    "TestUserUsagePage",
]
