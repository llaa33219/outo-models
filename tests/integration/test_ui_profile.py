"""Integration tests for the profile page rework + color tinting.

Covers:
    * The profile page renders the two big tiles (profile + repos) in
      the right DOM order, with bio / interests / links seeded via the
      JSON profile endpoint.
    * Recent activity shows both a "created" row and a "pushed" row
      after seeding a repo + a push (via the upload endpoint).
    * The edit form is owner-only: anonymous → redirect, stranger → 403,
      owner → 200; CSRF-protected on POST.
    * POST /{username}/edit saves the changes and reflects them on the
      profile page; bad link URLs surface an in-page error and re-render
      the form with the typed values preserved.
    * POST /new persists the chosen color; the catalog card and repo
      page header carry an inline `style="background-color: ..."`.
    * POST /{owner}/{name}/color clears + changes the color; the tint
      attribute disappears when no color is set.
    * All new POSTs reject mismatched CSRF tokens with 403.

The harness pattern mirrors `test_ui_pages`: the `app` and
`seed_approved_user` fixtures from `tests/integration/conftest.py`
spin a fresh `FastAPI` per test against a tmpdir SQLite DB.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _form_csrf(client: TestClient, path: str) -> str:
    """GET `path` and return the CSRF field value matching the cookie."""
    response = client.get(path)
    assert response.status_code == 200, path
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None, path
    assert match.group(1) == cookie_token, path
    return cookie_token


async def _seed_repo_with_upload(client: TestClient, *, name: str = "colored") -> None:
    """Create a repo via the JSON API; one upload seeds a Revision row."""
    client.post(
        "/api/repos",
        json={"name": name, "kind": "model", "visibility": "public"},
    )
    upload = client.post(
        f"/api/repos/alice/{name}/upload",
        data={"message": "first commit"},
        files={"files": ("hello.txt", b"hello", "text/plain")},
    )
    assert upload.status_code == 200, upload.text


# ---------------------------------------------------------------------------
# Profile page (GET /{username})
# ---------------------------------------------------------------------------


class TestProfilePageLayout:
    """The profile page renders a left profile tile + a right repos tile."""

    async def test_profile_renders_two_tiles_in_order(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_repo_with_upload(client)

        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text

        # Both tile anchors are present in the layout grid.
        assert 'class="profile-layout"' in body
        assert 'class="profile-tile"' in body
        assert 'class="profile-repos"' in body

        # Left tile appears before the right tile in the markup so the
        # visual split matches the design.
        left = body.find('class="profile-tile"')
        right = body.find('class="profile-repos"')
        assert left != -1 and right != -1
        assert left < right, "profile tile must render before repos tile"

        # Avatar + display name + handle are all in the left tile.
        assert "alice" in body.lower()
        assert ">A<" in body or 'class="avatar lg"' in body
        assert "@alice" in body

    async def test_profile_renders_bio_interests_links(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        seeded = client.post(
            "/api/users/me/profile",
            json={
                "display_name": "Alice Z.",
                "bio": "Building tiny models for tiny devices.",
                "interests": ["nlp", "vision"],
                "links": [
                    {"label": "blog", "url": "https://alice.example.com"},
                    {"label": "github", "url": "https://github.com/alice"},
                ],
            },
        )
        assert seeded.status_code == 200, seeded.text
        client.post("/api/auth/logout")

        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text

        # Display name wins over the username on the profile card.
        assert "Alice Z." in body
        # Bio + interests + links all surface in the left tile.
        assert "Building tiny models" in body
        # Two `<span class="pill ...">` entries for the interests.
        assert body.count('class="pill"') >= 2
        # Each external link renders an `target="_blank"` anchor.
        for needle in (
            'href="https://alice.example.com"',
            'href="https://github.com/alice"',
            'target="_blank"',
            'rel="noopener noreferrer"',
        ):
            assert needle in body, needle

    async def test_recent_activity_shows_created_and_pushed(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_repo_with_upload(client, name="colored")
        client.post("/api/auth/logout")

        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text

        # The activity section + the two activity rows are present.
        assert "Recent activity" in body
        assert "/alice/colored" in body
        # The "created" and "pushed" chips both render with distinct
        # classes (the chip text uses an inner span; the BLP palette
        # chip background is set by CSS only — we anchor on the class).
        assert "profile-tile__activity-chip--repo" in body
        assert "profile-tile__activity-chip--push" in body

    async def test_no_color_repo_has_no_tint_style(
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
            json={"name": "plain", "kind": "model", "visibility": "public"},
        )

        # Profile page catalog card has no `background-color:` style.
        profile = client.get("/alice")
        body = profile.text
        card_block = body.split('<article class="card"', 1)[1].split("</article>", 1)[0]
        assert "background-color:" not in card_block

        # Repo header tile has no inline style either.
        view = client.get("/alice/plain")
        header_block = view.text.split('<section class="repo-header"', 1)[1].split("</section>", 1)[
            0
        ]
        assert "background-color:" not in header_block


# ---------------------------------------------------------------------------
# Edit profile (GET + POST /{username}/edit)
# ---------------------------------------------------------------------------


class TestProfileEditPage:
    """`/{username}/edit` is owner-only, CSRF-protected, and saves fields."""

    async def test_edit_anonymous_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        response = client.get("/alice/edit", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert "next=" in response.headers["location"]

    async def test_edit_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.get("/alice/edit", follow_redirects=False)
        assert response.status_code == 403

    async def test_edit_owner_renders_form_with_current_values(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/users/me/profile",
            json={
                "display_name": "Alice Z.",
                "bio": "Bio seed.",
                "interests": ["nlp"],
                "links": [{"label": "home", "url": "https://alice.example.com"}],
            },
        )

        response = client.get("/alice/edit")
        assert response.status_code == 200
        body = response.text
        assert 'value="Alice Z."' in body
        assert "Bio seed." in body
        assert "nlp" in body
        # The first link row is pre-filled; the remaining 7 are blank.
        assert 'name="link_label_0"' in body
        assert 'value="home"' in body
        assert 'name="link_url_0"' in body
        assert 'value="https://alice.example.com"' in body

    async def test_edit_post_without_csrf_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/alice/edit",
            data={"display_name": "New", "bio": "", "interests": ""},
        )
        assert response.status_code == 403

    async def test_edit_post_saves_and_redirects(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/alice/edit")

        response = client.post(
            "/alice/edit",
            data={
                "_csrf": csrf,
                "display_name": "Alice Z.",
                "bio": "New bio.",
                "interests": "nlp, vision",
                "link_label_0": "home",
                "link_url_0": "https://alice.example.com",
                "link_label_1": "",
                "link_url_1": "",
                "link_label_2": "",
                "link_url_2": "",
                "link_label_3": "",
                "link_url_3": "",
                "link_label_4": "",
                "link_url_4": "",
                "link_label_5": "",
                "link_url_5": "",
                "link_label_6": "",
                "link_url_6": "",
                "link_label_7": "",
                "link_url_7": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/alice"

        # Profile reflects the new values via the JSON endpoint.
        view = client.get("/api/users/alice").json()
        assert view["display_name"] == "Alice Z."
        assert view["bio"] == "New bio."
        assert view["interests"] == ["nlp", "vision"]
        assert view["links"] == [{"label": "home", "url": "https://alice.example.com"}]

        # Profile page HTML also surfaces them.
        profile = client.get("/alice")
        assert "Alice Z." in profile.text
        assert "New bio." in profile.text

    async def test_edit_post_invalid_link_rerenders_with_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/alice/edit")

        response = client.post(
            "/alice/edit",
            data={
                "_csrf": csrf,
                "display_name": "Alice",
                "bio": "",
                "interests": "",
                "link_label_0": "bad",
                "link_url_0": "ftp://nope.example.com",
                "link_label_1": "",
                "link_url_1": "",
                "link_label_2": "",
                "link_url_2": "",
                "link_label_3": "",
                "link_url_3": "",
                "link_label_4": "",
                "link_url_4": "",
                "link_label_5": "",
                "link_url_5": "",
                "link_label_6": "",
                "link_url_6": "",
                "link_label_7": "",
                "link_url_7": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        body = response.text
        # In-page error block + the typed values are preserved.
        assert 'class="errors"' in body
        assert "http" in body.lower()
        assert "ftp://nope.example.com" in body
        assert "Alice" in body

    async def test_edit_post_oversized_bio_rerenders_with_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/alice/edit")

        response = client.post(
            "/alice/edit",
            data={
                "_csrf": csrf,
                "display_name": "Alice",
                "bio": "x" * 2001,
                "interests": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        body = response.text
        assert 'class="errors"' in body
        assert "2000" in body

    async def test_edit_post_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/alice/edit",
            data={"_csrf": "anything", "display_name": "X", "bio": "", "interests": ""},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Repo accent color (POST /{owner}/{name}/color, /new with color)
# ---------------------------------------------------------------------------


class TestRepoColorTint:
    """The repo color tints cards + the repo header, and is owner-editable."""

    async def test_new_with_color_persists_and_tints_card(
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
                "name": "tinted-model",
                "visibility": "public",
                "description": "",
                "color": "#DBEDFF",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/alice/tinted-model"

        # Repo header tile carries the tint.
        view = client.get("/alice/tinted-model")
        assert view.status_code == 200
        header_block = view.text.split('<section class="repo-header"', 1)[1].split("</section>", 1)[
            0
        ]
        assert "color-mix(in srgb, #dbedff 14%, var(--tile-bg))" in header_block.lower()

        # Catalog card carries the tint too.
        models = client.get("/models")
        card_block = models.text.split('<article class="card"', 1)[1].split("</article>", 1)[0]
        assert "background-color:" in card_block

        # The profile page's repo card is tinted too.
        profile = client.get("/alice")
        profile_card = profile.text.split('<article class="card"', 1)[1].split("</article>", 1)[0]
        assert "background-color:" in profile_card

    async def test_new_without_color_persists_none_and_omits_tint(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/new")
        client.post(
            "/new",
            data={
                "_csrf": csrf,
                "kind": "model",
                "name": "plain-model",
                "visibility": "public",
                "description": "",
                "color": "",
            },
            follow_redirects=False,
        )

        view = client.get("/alice/plain-model")
        header_block = view.text.split('<section class="repo-header"', 1)[1].split("</section>", 1)[
            0
        ]
        assert "background-color:" not in header_block

        profile = client.get("/alice")
        # Find the card for plain-model specifically.
        plain_card_re = (
            r'<article class="card"[^>]*>'
            r"(?:(?!</article>).)*?plain-model"
            r"(?:(?!</article>).)*?</article>"
        )
        plain_card_match = re.search(plain_card_re, profile.text, flags=re.DOTALL)
        assert plain_card_match is not None
        assert "background-color:" not in plain_card_match.group(0)

    async def test_color_picker_form_changes_color(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )

        csrf = _form_csrf(client, "/alice/switchable")
        response = client.post(
            "/alice/switchable/color",
            data={"_csrf": csrf, "color": "#B8E1D8"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        view = client.get("/alice/switchable")
        header_block = view.text.split('<section class="repo-header"', 1)[1].split("</section>", 1)[
            0
        ]
        assert "background-color:" in header_block
        assert "#b8e1d8" in header_block.lower()

    async def test_color_picker_clears_color(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        # Set first.
        csrf = _form_csrf(client, "/alice/switchable")
        client.post(
            "/alice/switchable/color",
            data={"_csrf": csrf, "color": "#DBEDFF"},
            follow_redirects=False,
        )
        # Then clear.
        csrf = _form_csrf(client, "/alice/switchable")
        client.post(
            "/alice/switchable/color",
            data={"_csrf": csrf, "color": ""},
            follow_redirects=False,
        )

        view = client.get("/alice/switchable")
        header_block = view.text.split('<section class="repo-header"', 1)[1].split("</section>", 1)[
            0
        ]
        assert "background-color:" not in header_block

    async def test_color_picker_invalid_color_redirects_with_error(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        csrf = _form_csrf(client, "/alice/switchable")
        response = client.post(
            "/alice/switchable/color",
            data={"_csrf": csrf, "color": "not-a-color"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        # The redirect carries the validation error in the query string
        # so the header picker re-renders with an in-page error.
        assert "color_error=" in response.headers["location"]

        # Following the redirect surfaces the error in the picker tile.
        view = client.get(response.headers["location"])
        assert view.status_code == 200
        assert 'class="errors"' in view.text

    async def test_color_picker_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/repos",
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/alice/switchable/color",
            data={"_csrf": "any", "color": "#DBEDFF"},
        )
        assert response.status_code == 403

    async def test_color_picker_without_csrf_is_403(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        response = client.post(
            "/alice/switchable/color",
            data={"color": "#DBEDFF"},
        )
        assert response.status_code == 403

    async def test_color_picker_anon_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/repos",
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        response = client.post(
            "/alice/switchable/color",
            data={"_csrf": "any", "color": "#DBEDFF"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    async def test_repo_page_renders_picker_for_owner_only(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )

        owner = client.get("/alice/switchable")
        assert 'class="repo-color-picker el-tile"' in owner.text
        assert "Accent color" in owner.text

    async def test_repo_page_picker_absent_for_anon(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        anon = client.get("/alice/switchable")
        assert 'class="repo-color-picker el-tile"' not in anon.text

    async def test_repo_page_picker_absent_for_stranger(
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
            json={"name": "switchable", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        stranger = client.get("/alice/switchable")
        assert 'class="repo-color-picker el-tile"' not in stranger.text


# ---------------------------------------------------------------------------
# Profile-page color tint on cards + the new GET /new form's palette select
# ---------------------------------------------------------------------------


class TestNewFormPalette:
    """The /new GET form exposes the curated palette as a select."""

    async def test_new_form_lists_palette_options(
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
        # "None" option is present + at least one hex.
        assert 'value=""' in body
        for hex_value in ("#DBEDFF", "#B8E1D8", "#D67FFF"):
            assert hex_value in body


__all__ = [
    "TestNewFormPalette",
    "TestProfileEditPage",
    "TestProfilePageLayout",
    "TestRepoColorTint",
]
