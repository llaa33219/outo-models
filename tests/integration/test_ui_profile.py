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
import shutil
from pathlib import Path

from dulwich import porcelain
from fastapi import FastAPI
from fastapi.testclient import TestClient

from outo_models.repos.storage import repo_fs_path

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


def _seed_bare_repo(
    tmp_data_dir: Path,
    *,
    owner: str,
    name: str,
    files: dict[str, str],
) -> None:
    """Build a bare repo with the supplied files (committed to `main`).

    Mirror of the helper used by `test_ui_repo_page`: the bare slot
    created by the create-repo API is replaced with a fresh clone of a
    temp worktree so the page reads exactly what's on disk.
    """
    work = tmp_data_dir / "src-repo"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    for rel_path, content in files.items():
        target = work / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    porcelain.init(str(work))
    porcelain.add(str(work), paths=list(files.keys()))
    porcelain.commit(
        str(work),
        message=b"init",
        author=b"alice <a@example.com>",
        committer=b"alice <a@example.com>",
    )
    bare = repo_fs_path(owner, name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    if bare.exists():
        shutil.rmtree(bare)
    porcelain.clone(str(work), str(bare), bare=True)


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


# ---------------------------------------------------------------------------
# v0.5.6 — Profile README (`<username>/<username>` repo acts as the README).
# ---------------------------------------------------------------------------


class TestProfileReadme:
    """The `<username>/<username>` repo renders as the profile README.

    Renders only when:
        - the `<username>/<username>` repo exists for that user,
        - the repo's default branch has a README.md at the root,
        - the repo is visible to the viewer (public, or owned by self/admin).

    Otherwise the profile page renders no README tile (no error).
    """

    async def test_profile_readme_renders_when_username_username_has_readme(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        # Create the `<username>/<username>` repo via the API.
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "alice", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="alice",
            files={
                "README.md": "# Hello\n\nThis is **alice**'s profile readme.\n",
            },
        )

        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        # The README tile renders inside `article.profile-readme`. The
        # BLP tile chrome lives in profile.html's page-local CSS, so
        # the class name is profile-local (not the repo card class).
        assert 'class="profile-readme' in body
        # Body is sanitized + rendered as HTML through the same card
        # pipeline the model card tab uses.
        assert "Hello" in body
        assert "<strong>alice</strong>" in body or "<b>alice</b>" in body

    async def test_profile_readme_absent_when_no_username_repo(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        # No `<username>/<username>` repo created — profile renders without the tile.
        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        # No `article.profile-readme` element rendered. We anchor on the
        # class-with-tag combination so the test does not match the CSS
        # selectors inside the inline `<style>` block.
        assert 'class="profile-readme' not in body
        # The right repos tile is still present (Models / Datasets / Spaces).
        assert 'class="profile-repos"' in body

    async def test_profile_readme_absent_when_readme_missing(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "alice", "kind": "model", "visibility": "public"},
        )
        # Repo exists, but no README.md at the root.
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="alice",
            files={"other.txt": "no readme here"},
        )

        response = client.get("/alice")
        body = response.text
        assert 'class="profile-readme' not in body

    async def test_profile_readme_sanitizes_script_tags(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "alice", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="alice",
            files={
                # README.md with a script injection attempt — the sanitizer
                # from `repos.card` MUST strip the <script> tag.
                "README.md": "# Title\n<script>alert('xss')</script>\n",
            },
        )

        response = client.get("/alice")
        body = response.text
        assert 'class="profile-readme' in body
        assert "<script>alert" not in body
        # The benign text still renders.
        assert "Title" in body

    async def test_profile_readme_hidden_for_stranger_when_private(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
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
            json={"name": "alice", "kind": "model", "visibility": "private"},
        )
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="alice",
            files={"README.md": "# Private bio\n"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")

        response = client.get("/alice")
        body = response.text
        # Private profile-README repo → tile is hidden for strangers
        # (no error, no leaked existence).
        assert 'class="profile-readme' not in body

    async def test_profile_readme_visible_for_owner_when_private(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "alice", "kind": "model", "visibility": "private"},
        )
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="alice",
            files={"README.md": "# Owner-only bio\n"},
        )

        response = client.get("/alice")
        body = response.text
        # Owner sees their own private README.
        assert 'class="profile-readme' in body
        assert "Owner-only bio" in body


def _login(client: TestClient, username: str) -> None:
    """Log `username` in via the JSON API."""
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


__all__ = [
    "TestNewFormPalette",
    "TestProfileEditPage",
    "TestProfilePageLayout",
    "TestProfileReadme",
    "TestRepoColorTint",
]
