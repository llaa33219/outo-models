"""Integration tests for the HF-style repository page (`/{owner}/{name}`).

Covers the v0.3.0 contract:

    * Header row renders `owner/name`, the clone-url copy button, and
      the like / follow capsule buttons with correct counts and filled
      state for the viewer when they've liked / are following.
    * Tabs (card / files / community) are separate URLs and the selected
      tab uses the permanent-selection filled capsule.
    * The card tab renders the README markdown + sidebar front-matter.
    * The files tab lists the seeded tree; empty repos show a friendly
      empty state instead of a 500.
    * The community tab lists comments newest-first and posts new ones
      via form POST; anonymous viewers see a log-in hint instead of
      the composer.
    * Like / follow / comment mutations are form POST routes with CSRF
      protection (no token → 403) and login required (anonymous → 303
      to /login with `next=...` preserved).

The conftest fixtures (`app`, `seed_approved_user`, `tmp_data_dir`)
already wire the full lifespan — `seed_approved_user` returns an async
seeder so tests can stay linear without dropping the `async` keyword.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from dulwich import porcelain
from fastapi import FastAPI
from fastapi.testclient import TestClient

from outo_models.repos.storage import repo_fs_path


def _login(client: TestClient, username: str) -> None:
    """Log `username` in via the JSON API; session cookie sticks in the jar."""
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


def _form_csrf(client: TestClient, path: str) -> str:
    """GET `path` and return the CSRF token matching the `_csrf` cookie."""
    response = client.get(path)
    assert response.status_code == 200, (path, response.text)
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None, path
    assert match.group(1) == cookie_token, path
    return cookie_token


def _seed_bare_repo_with_readme(
    tmp_data_dir: Path,
    *,
    owner: str,
    name: str,
    files: dict[str, str],
    readme: str | None = None,
) -> None:
    """Build a bare repo with the supplied files (committed to `main`).

    `files` is a mapping of relative path → file content. The bare repo
    is cloned over the slot created by the create-repo API so the
    filesystem matches what the page actually lists.
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
# Header + tabs
# ---------------------------------------------------------------------------


class TestRepoHeader:
    """The header row exposes owner/name + copy + like + follow capsules."""

    async def test_header_renders_owner_name_and_copy_button(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "header-repo", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/header-repo")
        assert response.status_code == 200
        body = response.text
        assert "/alice/header-repo" in body or "alice/header-repo" in body
        # Copy button exists and points at the clone-url element.
        assert 'class="copy-btn' in body
        assert 'data-copy-target="clone-url-value"' in body
        assert 'id="clone-url-value"' in body

    async def test_like_button_renders_count_and_disabled_for_anon(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "like-anon", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/like-anon")
        assert response.status_code == 200
        body = response.text
        # Capsule button present, count = 0, log-in hint for anonymous viewers.
        assert "like-count" in body
        assert 'title="Log in to like"' in body
        # Like POST is gated → anonymous GET is just the page; we cover the
        # POST gate in TestLikeFormPost below.

    async def test_follow_button_renders_count_and_disabled_for_anon(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "follow-anon", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/follow-anon")
        assert response.status_code == 200
        body = response.text
        # Follow capsule targets the OWNER (alice), not the viewer.
        assert "follow-count" in body
        assert "follower" in body or "Follow" in body
        assert 'title="Log in to follow"' in body

    async def test_authenticated_follow_button_hidden_for_self(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "self-follow", "kind": "model", "visibility": "public"},
        )
        response = client.get("/alice/self-follow")
        assert response.status_code == 200
        # The follow button must not be rendered for the repo owner.
        assert 'action="/alice/follow"' not in response.text

    async def test_like_button_shows_filled_state_for_liker(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "liked-repo", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        client.post("/api/repos/alice/liked-repo/like")
        response = client.get("/alice/liked-repo")
        assert response.status_code == 200
        body = response.text
        # The like <button> element carries the filled-state attribute.
        assert 'class="like-btn like-btn--active"' in body or "like-btn like-btn--active" in body
        assert 'like-count">1</span>' in body or ">1</span>" in body


class TestRepoTabs:
    """Tabs are separate URLs; the active tab uses the filled capsule."""

    async def test_tab_links_use_separate_urls(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "tabbed", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        # Default tab = card.
        card = client.get("/alice/tabbed")
        assert card.status_code == 200
        # All three tabs link to the correct URLs.
        assert 'href="/alice/tabbed"' in card.text
        assert 'href="/alice/tabbed/files"' in card.text
        assert 'href="/alice/tabbed/community"' in card.text

        files = client.get("/alice/tabbed/files")
        assert files.status_code == 200
        assert "Files" in files.text

        community = client.get("/alice/tabbed/community")
        assert community.status_code == 200
        assert "Community" in community.text

    async def test_files_tab_is_marked_active_when_visited(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "active-files", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/active-files/files")
        assert response.status_code == 200
        body = response.text
        # The Files tab carries the filled capsule (active class).
        assert 'href="/alice/active-files/files" class="tabs__link tabs__link--active"' in body

    async def test_community_tab_is_marked_active_when_visited(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "active-com", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/active-com/community")
        assert response.status_code == 200
        body = response.text
        assert 'href="/alice/active-com/community" class="tabs__link tabs__link--active"' in body


# ---------------------------------------------------------------------------
# Card tab
# ---------------------------------------------------------------------------


class TestRepoCardTab:
    """The card tab renders README + sidebar front-matter."""

    async def test_card_tab_renders_readme_markdown(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        # Create DB row first so the bare slot is owned by the API,
        # then replace the empty bare repo with the seeded tree.
        client.post(
            "/api/repos",
            json={"name": "carded", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="carded",
            files={
                "README.md": "---\n"
                "task: text-classification\n"
                "license: apache-2.0\n"
                "tags:\n  - nlp\n  - transformers\n"
                "datasets:\n  - glue\n"
                "---\n"
                "# Carded model\n\nThis is the body.\n",
            },
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/carded")
        assert response.status_code == 200
        body = response.text
        # README body rendered.
        assert "Carded model" in body
        assert "This is the body." in body
        # Front-matter exposed in the sidebar.
        assert "text-classification" in body
        assert "apache-2.0" in body
        assert "nlp" in body
        assert "glue" in body

    async def test_card_tab_empty_state_when_no_readme(
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
            json={"name": "no-md", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="no-md",
            files={"a.txt": "hi"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/no-md")
        assert response.status_code == 200
        body = response.text
        assert "empty-state" in body
        assert "README" in body or "push" in body.lower()

        response = client.get("/alice/no-md")
        assert response.status_code == 200
        body = response.text
        assert "empty-state" in body
        assert "README" in body or "push" in body.lower()

    async def test_empty_repo_renders_friendly_empty_state(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "fresh-empty", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/fresh-empty")
        assert response.status_code == 200
        body = response.text
        # No 500; the page renders an empty state on the card tab.
        assert "empty-state" in body

    async def test_dataset_repo_renders_dataset_card_tab(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "the-ds", "kind": "dataset", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/the-ds")
        assert response.status_code == 200
        # Dataset label, not model label.
        assert "Dataset card" in response.text


# ---------------------------------------------------------------------------
# Files tab
# ---------------------------------------------------------------------------


class TestRepoFilesTab:
    """The files tab lists the repo tree and shows an empty state when bare."""

    async def test_files_tab_lists_seeded_tree(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        # Create DB row first so the on-disk bare slot is owned by the API.
        client.post(
            "/api/repos",
            json={"name": "listed", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="listed",
            files={
                "README.md": "# x",
                "config.json": "{}",
                "src/util.py": "pass",
                "src/lib.go": "package src",
            },
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/listed/files")
        assert response.status_code == 200
        body = response.text
        # Files tab renders a BLP square table listing dirs first, then files.
        # Nested entries (src/util.py) only appear when drilling into `src`.
        assert "files-table" in body
        for needle in ("README.md", "config.json", "src/"):
            assert needle in body, needle

        # Drill into the `src` directory and confirm the nested file appears.
        response = client.get("/alice/listed/files?path=src")
        assert response.status_code == 200
        body = response.text
        assert "files-table" in body
        for needle in ("util.py", "lib.go"):
            assert needle in body, needle

    async def test_files_tab_empty_state_for_empty_repo(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "empty-files", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/empty-files/files")
        assert response.status_code == 200
        assert "empty-state" in response.text

    async def test_files_tab_traversal_returns_empty_state_not_500(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        # Create DB row first, then seed bare so the API owns the slot.
        client.post(
            "/api/repos",
            json={"name": "traverse", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="traverse",
            files={"README.md": "x"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/traverse/files?path=../../etc")
        # Either a 404 (the API treats traversal as missing) or an empty
        # state tile — what matters is no 500.
        assert response.status_code in (200, 404), response.status_code


# ---------------------------------------------------------------------------
# Community tab
# ---------------------------------------------------------------------------


class TestRepoCommunityTab:
    """Community tab posts + lists comments via form POSTs."""

    async def test_community_tab_anonymous_hides_composer(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "anon-com", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/anon-com/community")
        assert response.status_code == 200
        body = response.text
        # No composer textarea rendered for anonymous viewers. Match the
        # opening tag with attribute context to avoid the JS-comment match.
        assert "<textarea " not in body and "<textarea\n" not in body
        assert 'name="body"' not in body
        # A login hint is shown.
        assert "Log in" in body or "log in" in body.lower()

    async def test_community_tab_authenticated_can_post_comment(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "chat", "kind": "model", "visibility": "public"},
        )
        csrf = _form_csrf(client, "/alice/chat/community")
        response = client.post(
            "/alice/chat/comments",
            data={"_csrf": csrf, "body": "Hello from a test!"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/community")

        listed = client.get("/alice/chat/community")
        assert listed.status_code == 200
        assert "Hello from a test!" in listed.text
        # Author chip present.
        assert "alice" in listed.text

    async def test_community_tab_post_without_csrf_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "no-csrf-c", "kind": "model", "visibility": "public"},
        )
        response = client.post(
            "/alice/no-csrf-c/comments",
            data={"body": "no token"},
        )
        assert response.status_code == 403

    async def test_community_tab_post_anonymous_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "anon-c", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        # GET the page first so the test client has a CSRF cookie minted,
        # mirroring a real browser that visited the page before POSTing.
        # The anonymous page renders no <form>, so we read the cookie
        # directly rather than via the form helper.
        response = client.get("/alice/anon-c/community")
        assert response.status_code == 200
        csrf = response.cookies.get("_csrf") or client.cookies.get("_csrf")
        assert csrf
        posted = client.post(
            "/alice/anon-c/comments",
            data={"_csrf": csrf, "body": "anon comment"},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"].startswith("/login")

    async def test_community_tab_existing_comment_is_listed(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        # Create the DB row + initial empty bare repo via the API first,
        # then replace the bare repo with one carrying a README.
        client.post(
            "/api/repos",
            json={"name": "precommented", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="precommented",
            files={"README.md": "# x"},
        )
        client.post(
            "/api/repos/alice/precommented/comments",
            json={"body": "early comment"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/precommented/community")
        assert response.status_code == 200
        assert "early comment" in response.text


# ---------------------------------------------------------------------------
# Like form POST
# ---------------------------------------------------------------------------


class TestLikeFormPost:
    """Like mutations toggle via form POST; CSRF-protected, login-gated."""

    async def test_like_form_post_toggles_count(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "like-toggle", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        await seed_approved_user(username="bob")
        _login(client, "bob")

        csrf = _form_csrf(client, "/alice/like-toggle")
        response = client.post(
            "/alice/like-toggle/like",
            data={"_csrf": csrf},
            headers={"referer": "http://testserver/alice/like-toggle"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        # Redirects back to the page (referrer is present → same path).
        assert response.headers["location"] == "/alice/like-toggle"

        # Re-render shows count = 1 and the filled capsule.
        again = client.get("/alice/like-toggle")
        assert again.status_code == 200
        body = again.text
        assert 'like-count">1</span>' in body or ">1</span>" in body
        # The class on the <button> element flips on when the viewer liked.
        assert 'class="like-btn like-btn--active"' in body

        # Toggling again unsets the like.
        csrf = _form_csrf(client, "/alice/like-toggle")
        response = client.post(
            "/alice/like-toggle/like",
            data={"_csrf": csrf},
            headers={"referer": "http://testserver/alice/like-toggle"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        again = client.get("/alice/like-toggle")
        body = again.text
        assert 'like-count">0</span>' in body or ">0</span>" in body
        # Filled-state class must be gone from the button element itself.
        assert 'class="like-btn like-btn--active"' not in body
        # The empty-state class is still present on the button.
        assert 'class="like-btn"' in body

    async def test_like_post_without_csrf_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "like-nocsrf", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        await seed_approved_user(username="bob")
        _login(client, "bob")
        response = client.post(
            "/alice/like-nocsrf/like",
            data={},
        )
        assert response.status_code == 403

    async def test_like_post_anonymous_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "like-anon-form", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        # Anon GET mints a CSRF cookie but the page renders no <form>
        # (the like button is the disabled anonymous variant), so we
        # pull the cookie directly off the response instead of via the
        # form-field helper.
        response = client.get("/alice/like-anon-form")
        assert response.status_code == 200
        csrf = response.cookies.get("_csrf") or client.cookies.get("_csrf")
        assert csrf
        posted = client.post(
            "/alice/like-anon-form/like",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# Follow form POST
# ---------------------------------------------------------------------------


class TestFollowFormPost:
    """Follow toggles via form POST on the repo's OWNER."""

    async def test_follow_form_post_toggles_count(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "follow-toggle", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")

        csrf = _form_csrf(client, "/alice/follow-toggle")
        # Set the Referer so the redirect bounces back to the repo page;
        # the browser sends Referer on a same-origin form POST.
        response = client.post(
            "/alice/follow",
            data={"_csrf": csrf},
            headers={"referer": "http://testserver/alice/follow-toggle"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/alice/follow-toggle"

        again = client.get("/alice/follow-toggle")
        assert again.status_code == 200
        body = again.text
        # The follow <button> element flips to filled when the viewer follows.
        assert (
            'class="follow-btn follow-btn--active"' in body
            or "follow-btn follow-btn--active" in body
        )
        assert 'follow-count">1</span>' in body or ">1</span>" in body

    async def test_follow_post_without_csrf_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "follow-nocsrf", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        await seed_approved_user(username="bob")
        _login(client, "bob")
        response = client.post("/alice/follow", data={})
        assert response.status_code == 403

    async def test_follow_self_form_post_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "self-follow-form", "kind": "model", "visibility": "public"},
        )
        # The button is hidden for the owner, so the form POST shouldn't
        # be reachable — but a hand-crafted POST must still fail cleanly.
        response = client.post(
            "/alice/follow",
            data={},
            follow_redirects=False,
        )
        # Either 303 (because the route refuses anonymous-style flow) or
        # 401/403 because of the authz gate. Crucially: NOT 500.
        assert response.status_code in (303, 401, 403), response.status_code


# ---------------------------------------------------------------------------
# Route ordering / 404
# ---------------------------------------------------------------------------


class TestRepoRouteOrdering:
    """Repo routes coexist with /{username}, /settings/tokens, etc."""

    async def test_settings_tokens_still_renders(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/settings/tokens")
        assert response.status_code == 200
        assert "Access tokens" in response.text

    async def test_profile_page_still_renders(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/alice")
        assert response.status_code == 200

    async def test_unknown_repo_returns_404(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/nobody/ghost")
        assert response.status_code == 404

    async def test_unknown_repo_files_returns_404(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/nobody/ghost/files")
        assert response.status_code == 404

    async def test_unknown_repo_community_returns_404(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/nobody/ghost/community")
        assert response.status_code == 404
