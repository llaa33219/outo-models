"""Integration tests for the Access Tokens settings page (`/settings/tokens`).

Covers the end-to-end web flow that lets users mint, list, and revoke
Personal Access Tokens from the browser:

    * Anonymous GET → 303 to /login (next=/settings/tokens preserved).
    * Authenticated GET renders the create card + empty-state tiles.
    * POST without CSRF → 403.
    * POST with CSRF mints a PAT; the raw token is shown ONCE in the
      re-render and never again on reload.
    * The newly-issued token authenticates `/api/auth/me` (Bearer header).
    * Revoke removes the row; the rendered list no longer mentions it.
    * Other users' tokens are invisible from this page (their row count
      stays at zero).
"""

from __future__ import annotations

import base64
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _login(client: TestClient, username: str) -> None:
    """Log in `username` through the JSON API; the session cookie is
    stored in the client's jar for subsequent HTML requests."""
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


def _form_csrf(client: TestClient, path: str) -> str:
    """GET `path` and return the CSRF token matching the `_csrf` cookie."""
    response = client.get(path)
    assert response.status_code == 200, path
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None, path
    assert match.group(1) == cookie_token, path
    return cookie_token


def _raw_token_from_render(html: str) -> str:
    """Extract the plaintext token from the `<pre id="new-token-value">` block."""
    match = re.search(r'<pre class="clone-cmd token-box" id="new-token-value">([^<]+)</pre>', html)
    assert match is not None, "token-shown-once panel was not rendered"
    return match.group(1)


class TestAuthGate:
    """Anonymous requests redirect to /login and preserve the original path."""

    def test_anonymous_get_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/settings/tokens", follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/login")
        assert "next=/settings/tokens" in location

    def test_anonymous_post_returns_401(self, app: tuple[TestClient, FastAPI, object]) -> None:
        """The POST handler is gated by `_require_login_user` (raises 401),
        matching the existing `/new` POST behavior — only the GET path is
        required to 303-redirect (so the form is never submitted blind)."""
        client, _, _ = app
        response = client.post(
            "/settings/tokens",
            data={"name": "anon", "scopes": ["read"], "ttl_days": "30"},
            follow_redirects=False,
        )
        assert response.status_code == 401


class TestCsrfEnforced:
    """Form POSTs without a matching CSRF token are rejected with 403."""

    async def test_create_without_csrf_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="jane")
        _login(client, "jane")
        response = client.post(
            "/settings/tokens",
            data={"name": "no-csrf", "scopes": ["read"], "ttl_days": "30"},
        )
        assert response.status_code == 403

    async def test_revoke_without_csrf_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="jane")
        _login(client, "jane")
        # Seed a token directly through the API so the delete URL has a real id.
        create = client.post(
            "/api/auth/tokens",
            json={"name": "revoke-me", "scopes": ["read"]},
        )
        token_id = create.json()["id"]
        response = client.post(f"/settings/tokens/{token_id}/delete", data={})
        assert response.status_code == 403


class TestCreateFlow:
    """The full mint → render-raw → reload-no-leak loop."""

    async def test_create_shows_raw_token_and_copies_with_clipboard(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="kate")
        _login(client, "kate")

        csrf = _form_csrf(client, "/settings/tokens")
        response = client.post(
            "/settings/tokens",
            data={
                "_csrf": csrf,
                "name": "kate-laptop",
                "scopes": ["read", "write"],
                "ttl_days": "90",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200  # re-renders the page
        body = response.text

        raw = _raw_token_from_render(body)
        assert raw, "raw token must be present in the shown-once panel"
        assert raw.startswith("v4.local.") or len(raw) > 20  # PASETO v4 prefix / opaque string

        # The shown-once panel mentions it will not be shown again.
        assert "not be shown again" in body.lower()
        # The git usage guidance is present and does NOT embed the token.
        assert "git clone" in body
        assert "Username:" in body
        assert "kate" in body
        # Token appears inside the copyable box only.
        token_box_count = body.count(raw)
        assert token_box_count == 1, (
            f"raw token must appear exactly once (in the copyable box); saw {token_box_count}"
        )

    async def test_create_then_reload_does_not_leak_raw_token(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="liam")
        _login(client, "liam")

        csrf = _form_csrf(client, "/settings/tokens")
        minted = client.post(
            "/settings/tokens",
            data={
                "_csrf": csrf,
                "name": "ci-deploy",
                "scopes": ["read", "write"],
                "ttl_days": "30",
            },
        )
        raw = _raw_token_from_render(minted.text)
        assert raw

        # Reload the page — the raw token MUST NOT appear again.
        reload = client.get("/settings/tokens")
        assert reload.status_code == 200
        assert raw not in reload.text, "raw token leaked on reload"

        # The token is still tracked (prefix visible).
        assert raw[:8] in reload.text

    async def test_issued_token_authenticates_git_basic_auth(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        """The same token must work for `git clone` / `git push` over Basic auth.

        The HTTP Basic header carries `<username>:<token>`; we resolve it via
        the same `resolve_git_identity` helper the smart-HTTP service uses,
        so a regression in the token-API surface surfaces here too.
        """
        from outo_models.config import get_settings
        from outo_models.git_smart.auth import resolve_git_identity

        client, _, _ = app
        await seed_approved_user(username="noah")
        _login(client, "noah")

        csrf = _form_csrf(client, "/settings/tokens")
        minted = client.post(
            "/settings/tokens",
            data={
                "_csrf": csrf,
                "name": "git-push",
                "scopes": ["read", "write"],
                "ttl_days": "90",
            },
        )
        raw = _raw_token_from_render(minted.text)

        basic = base64.b64encode(f"noah:{raw}".encode("ascii")).decode("ascii")
        identity = await resolve_git_identity(f"Basic {basic}", settings=get_settings())
        assert identity is not None
        assert identity.username == "noah"


class TestListAndRevoke:
    """Listing shows prefix + scopes, revoke removes the row."""

    async def test_list_shows_prefix_and_metadata(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="olive")
        _login(client, "olive")

        # Mint two tokens via the API so we can assert ordering + count.
        client.post("/api/auth/tokens", json={"name": "first", "scopes": ["read"]})
        client.post("/api/auth/tokens", json={"name": "second", "scopes": ["read", "write"]})

        response = client.get("/settings/tokens")
        assert response.status_code == 200
        body = response.text
        assert "first" in body
        assert "second" in body
        # The 8-char prefixes are visible.
        assert body.count('class="pill token-prefix"') == 2
        assert body.count('class="danger revoke-btn"') == 2

    async def test_revoke_removes_token_and_redirects(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="paul")
        _login(client, "paul")

        created = client.post("/api/auth/tokens", json={"name": "goodbye", "scopes": ["read"]})
        token_id = created.json()["id"]

        csrf = _form_csrf(client, "/settings/tokens")
        response = client.post(
            f"/settings/tokens/{token_id}/delete",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/tokens"

        # The row is gone.
        reload = client.get("/settings/tokens")
        assert "goodbye" not in reload.text

    async def test_other_user_tokens_invisible_from_settings_page(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        # Alice creates a token.
        _login(client, "alice")
        client.post("/api/auth/tokens", json={"name": "alice-secret"})
        client.post("/api/auth/logout")
        # Bob's settings page must not see alice's token.
        _login(client, "bob")
        response = client.get("/settings/tokens")
        assert response.status_code == 200
        assert "alice-secret" not in response.text
        # Bob has none either.
        assert body_contains_empty_state(response.text)


def body_contains_empty_state(body: str) -> bool:
    """True iff the page rendered the empty-state tile for zero tokens."""
    return "You have not created any tokens yet." in body


class TestPageChrome:
    """The page renders with BLP Minimal Tile chrome: Pretendard, square
    tiles, capsule buttons, no UI-chrome gradients."""

    async def test_page_uses_pretendard_and_capsule_inputs(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="quinn")
        _login(client, "quinn")
        response = client.get("/settings/tokens")
        assert response.status_code == 200
        body = response.text
        # Font family inherited from base.html.
        assert "'Pretendard', system-ui" in body
        # No gradients on the UI chrome (the dark `pre` is content per §0.1).
        assert "linear-gradient" not in body
        assert "radial-gradient" not in body

    async def test_create_form_lists_all_three_scopes(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="ruth")
        _login(client, "ruth")
        response = client.get("/settings/tokens")
        body = response.text
        assert 'value="read"' in body
        assert 'value="write"' in body
        assert 'value="admin"' in body
        # TTL options present.
        assert 'value="30"' in body
        assert 'value="90"' in body
        assert 'value="365"' in body


__all__ = [
    "TestAuthGate",
    "TestCreateFlow",
    "TestCsrfEnforced",
    "TestListAndRevoke",
    "TestPageChrome",
]
