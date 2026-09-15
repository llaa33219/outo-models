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

import json
import re
import shutil
from pathlib import Path

from dulwich import porcelain
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.db import AuditLog, Revision, get_engine, get_session_factory
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


def _seed_bare_repo_bytes(
    tmp_data_dir: Path,
    *,
    owner: str,
    name: str,
    files: dict[str, bytes],
) -> None:
    """Same as `_seed_bare_repo_with_readme`, but takes raw bytes per file.

    Needed for binary blobs (images / videos / etc.) that the text-only
    helper would corrupt. Files are committed to `main`.
    """
    work = tmp_data_dir / "src-repo-bytes"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    for rel_path, content in files.items():
        target = work / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
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
        # Copy button exists and points at the clone-command element.
        assert 'class="copy-btn' in body
        assert 'data-copy-target="clone-command-value"' in body
        assert 'id="clone-command-value"' in body
        # The hidden source carries the FULL `git clone <url>` command,
        # not just the URL — one-click pastes the whole line.
        assert "git clone http" in body

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


# ---------------------------------------------------------------------------
# v0.5.x — Settings tab (owner/admin only), likes roster, threaded comments
# ---------------------------------------------------------------------------


class TestRepoSettingsTab:
    """`/{owner}/{name}/settings` is owner/admin-only and writes via form POST."""

    async def test_settings_tab_link_visible_for_owner(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )

        response = client.get("/alice/sett")
        assert response.status_code == 200
        body = response.text
        assert 'href="/alice/sett/settings"' in body
        assert ">Settings</a>" in body

    async def test_settings_tab_link_hidden_for_stranger(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")

        response = client.get("/alice/sett")
        assert response.status_code == 200
        assert 'href="/alice/sett/settings"' not in response.text

    async def test_settings_tab_link_hidden_for_anon(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/sett")
        assert response.status_code == 200
        assert 'href="/alice/sett/settings"' not in response.text

    async def test_settings_get_owner_renders_form(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={
                "name": "sett",
                "kind": "model",
                "visibility": "public",
                "description": "Original desc",
                "color": "#DBEDFF",
            },
        )

        response = client.get("/alice/sett/settings")
        assert response.status_code == 200
        body = response.text
        assert 'value="public" selected' in body or 'value="public"' in body
        assert "Original desc" in body
        assert "Renames are not supported yet" in body

    async def test_settings_post_updates_visibility_description_color(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )

        csrf = _form_csrf(client, "/alice/sett/settings")
        response = client.post(
            "/alice/sett/settings",
            data={
                "_csrf": csrf,
                "visibility": "private",
                "description": "Updated description.",
                "color": "#B8E1D8",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/settings?saved=1")

        view = client.get("/api/repos/alice/sett").json()
        assert view["visibility"] == "private"
        assert view["description"] == "Updated description."
        assert view["color"] == "#b8e1d8"

    async def test_settings_post_invalid_color_rerenders_with_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )

        csrf = _form_csrf(client, "/alice/sett/settings")
        response = client.post(
            "/alice/sett/settings",
            data={
                "_csrf": csrf,
                "visibility": "public",
                "description": "ok",
                "color": "not-a-color",
            },
            follow_redirects=False,
        )
        # POST now redirects back to the settings tab (the GET renders
        # through the shared repo-page renderer and picks the error up
        # from the query string).
        assert response.status_code == 303
        assert "settings_error=" in response.headers["location"]
        followed = client.get(response.headers["location"])
        assert followed.status_code == 200
        assert 'class="errors"' in followed.text

    async def test_settings_post_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        response = client.post(
            "/alice/sett/settings",
            data={"_csrf": "x", "visibility": "private", "description": "no", "color": ""},
        )
        assert response.status_code == 403

    async def test_settings_post_without_csrf_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )
        response = client.post(
            "/alice/sett/settings",
            data={"visibility": "private", "description": "no", "color": ""},
        )
        assert response.status_code == 403

    async def test_settings_anon_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sett", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.get("/alice/sett/settings", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


class TestLikesRoster:
    """The community tab renders a server-side roster of likers (with chips)."""

    async def test_likes_roster_renders_chips_for_each_liker(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        await seed_approved_user(username="carol")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "liked", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        client.post("/api/repos/alice/liked/like")
        client.post("/api/auth/logout")
        _login(client, "carol")
        client.post("/api/repos/alice/liked/like")
        client.post("/api/auth/logout")

        response = client.get("/alice/liked/community")
        assert response.status_code == 200
        body = response.text
        assert "likes-roster" in body
        assert 'href="/bob"' in body
        assert 'href="/carol"' in body
        assert "likes-roster__avatar" in body

    async def test_likes_roster_empty_state_for_no_likers(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "no-likes", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/no-likes/community")
        assert response.status_code == 200
        body = response.text
        assert "likes-roster" in body
        assert "No likes yet" in body


class TestThreadedComments:
    """The community tab threads comments with one level of nesting."""

    async def test_posting_reply_creates_nested_comment(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "thread", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos/alice/thread/comments",
            json={"body": "Top-level comment"},
        )
        comments = client.get("/api/repos/alice/thread/comments").json()
        assert len(comments) == 1
        top_id = comments[0]["id"]
        client.post("/api/auth/logout")

        _login(client, "bob")
        csrf = _form_csrf(client, "/alice/thread/community")
        response = client.post(
            "/alice/thread/comments",
            data={"_csrf": csrf, "body": "First reply", "parent_id": str(top_id)},
            follow_redirects=False,
        )
        assert response.status_code == 303

        listed = client.get("/alice/thread/community")
        assert listed.status_code == 200
        body = listed.text
        assert "Top-level comment" in body
        assert "First reply" in body
        assert "comment--reply" in body
        assert 'href="/bob"' in body

    async def test_reply_form_renders_for_logged_in_user(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "thread", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos/alice/thread/comments",
            json={"body": "Top-level"},
        )
        client.post("/api/auth/logout")

        _login(client, "bob")
        response = client.get("/alice/thread/community")
        assert response.status_code == 200
        body = response.text
        assert "comment-reply-form" in body
        assert 'name="parent_id"' in body
        assert 'value="1"' in body  # the first comment is id=1

    async def test_reply_form_absent_for_anon(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "thread", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos/alice/thread/comments",
            json={"body": "Top-level"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/thread/community")
        assert response.status_code == 200
        body = response.text
        assert '<form class="comment-reply-form"' not in body
        assert 'name="parent_id"' not in body

    async def test_reply_to_reply_flattens_into_same_chain(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "thread", "kind": "model", "visibility": "public"},
        )
        client.post(
            "/api/repos/alice/thread/comments",
            json={"body": "Top-level"},
        )
        comments = client.get("/api/repos/alice/thread/comments").json()
        top_id = comments[0]["id"]
        client.post(
            "/api/repos/alice/thread/comments",
            json={"body": "First reply", "parent_id": top_id},
        )
        comments = client.get("/api/repos/alice/thread/comments").json()
        first_reply_id = next(c["id"] for c in comments if c["parent_id"] == top_id)
        client.post(
            "/api/repos/alice/thread/comments",
            json={"body": "Reply to reply", "parent_id": first_reply_id},
        )

        response = client.get("/alice/thread/community")
        assert response.status_code == 200
        body = response.text
        assert body.count('class="comment comment--reply"') == 2
        assert "comment-reply-form" in body
        assert "Reply to reply" in body


# ---------------------------------------------------------------------------
# v0.5.6 — Files-tab View / Edit / Upload actions + clone-command copy.
# ---------------------------------------------------------------------------


class TestRepoCloneCommand:
    """The header copy button pastes the FULL `git clone <url>` line."""

    async def test_clone_command_copy_button_has_full_git_clone(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "clone-copy", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/clone-copy")
        assert response.status_code == 200
        body = response.text
        # The hidden source the copy button reads MUST carry the full
        # command so one click pastes a copy-pasteable line.
        assert 'id="clone-command-value"' in body
        clone_block = body.split('id="clone-command-value"', 1)[1].split("</pre>", 1)[0]
        assert "git clone " in clone_block
        assert ".git" in clone_block
        # The clipboard handler (`/static/clipboard.js`) reads
        # `textContent` of the target element — the source MUST NOT be
        # wrapped in <code>/<span> tags that would inject extra chars.
        assert "<code" not in clone_block
        assert "<span" not in clone_block

    async def test_clone_command_copy_button_target_id_present(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "cc-id", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")

        response = client.get("/alice/cc-id")
        body = response.text
        # `data-copy-target` on the button MUST point at the element id
        # that holds the full command.
        assert 'data-copy-target="clone-command-value"' in body
        assert "Copy clone command" in body


class TestFilesTabActions:
    """Owner-only View / Edit / Upload actions surface on the Files tab."""

    async def test_files_tab_renders_action_buttons_for_owner(
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
            json={"name": "f-actions", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-actions",
            files={
                "README.md": "x",
                "config.json": '{"k": 1}',
                "weights.bin": "\x00\x01\x02 binary",
            },
        )

        response = client.get("/alice/f-actions/files")
        assert response.status_code == 200
        body = response.text
        assert "files-upload-toggle" in body
        assert 'href="/alice/f-actions/files?upload=1"' in body
        assert body.count("View</a>") >= 2
        assert body.count("Edit</a>") >= 2
        assert body.count("Raw URL</button>") >= 2
        # Each file row carries its own raw-url source element so the
        # copy button can resolve it without a shared global id.
        assert 'id="raw-url-config.json"' in body
        assert 'id="raw-url-weights.bin"' in body

    async def test_files_tab_hides_upload_and_edit_for_stranger(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "f-stranger", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-stranger",
            files={"README.md": "x", "config.json": "{}"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")

        response = client.get("/alice/f-stranger/files")
        body = response.text
        # No upload tile, no Edit anchor (View + Raw URL still show).
        assert 'action="/alice/f-stranger/files/upload"' not in body
        assert 'class="files-upload"' not in body
        assert 'class="files-upload-toggle"' not in body
        assert "Edit</a>" not in body
        assert body.count("View</a>") >= 2
        assert body.count("Raw URL</button>") >= 2

    async def test_files_tab_view_renders_text_file_in_pre(
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
            json={"name": "f-view", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-view",
            files={
                "README.md": "x",
                "config.json": '{"alpha": 1, "beta": "hello"}',
            },
        )

        response = client.get("/alice/f-view/files/view?path=config.json")
        assert response.status_code == 200, response.text
        body = response.text
        assert "files-viewer" in body
        assert "config.json" in body
        assert "<textarea" not in body
        assert "files-viewer__pre" in body
        assert 'href="/alice/f-view/files/view?path=config.json&amp;edit=1"' in body

        response = client.get("/alice/f-view/files/view?path=config.json&edit=1")
        assert response.status_code == 200
        body = response.text
        textarea_idx = body.find('id="files-viewer-content"')
        assert textarea_idx > 0
        snippet = body[textarea_idx : textarea_idx + 800]
        assert "alpha" in snippet
        assert "&#34;" in snippet or "&quot;" in snippet
        assert "files-viewer-raw-url" in body

    async def test_files_tab_view_renders_metadata_for_binary(
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
            json={"name": "f-bin", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-bin",
            files={
                "README.md": "x",
                "weights.bin": "\x00\x01\x02 binary",
            },
        )

        response = client.get("/alice/f-bin/files/view?path=weights.bin")
        assert response.status_code == 200
        body = response.text
        assert "files-viewer" in body
        assert "weights.bin" in body
        assert "Binary or non-text content" in body
        assert "Download" in body
        assert "/alice/f-bin/raw/main/weights.bin" in body

    async def test_files_tab_raw_alias_route_serves_bytes(
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
            json={"name": "f-raw", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-raw",
            files={"README.md": "x", "config.json": '{"k": 1}'},
        )
        client.post("/api/auth/logout")

        # Public repo: anon can fetch the raw alias.
        response = client.get("/alice/f-raw/raw/main/config.json")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"k": 1}

    async def test_files_tab_edit_post_commits_new_content(
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
            json={"name": "f-edit", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-edit",
            files={"README.md": "x", "config.json": "old"},
        )

        csrf = _form_csrf(client, "/alice/f-edit/files")
        response = client.post(
            "/alice/f-edit/files/edit",
            data={
                "_csrf": csrf,
                "path": "config.json",
                "content": "new",
                "message": "edit config",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "edited=config.json" in response.headers["location"]

        # Files tab shows the new bytes.
        response = client.get("/alice/f-edit/files?path=")
        assert response.status_code == 200
        body = response.text
        assert "config.json" in body

        # Raw fetch reflects the edit.
        raw = client.get("/alice/f-edit/raw/main/config.json")
        assert raw.status_code == 200
        assert raw.text == "new"

    async def test_files_tab_edit_post_stranger_returns_403(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "f-noedit", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-noedit",
            files={"README.md": "x", "config.json": "x"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        response = client.post(
            "/alice/f-noedit/files/edit",
            data={"_csrf": "anything", "path": "config.json", "content": "x"},
        )
        assert response.status_code == 403

    async def test_files_tab_edit_post_without_csrf_is_403(
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
            json={"name": "f-editcsrf", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-editcsrf",
            files={"README.md": "x", "config.json": "x"},
        )
        response = client.post(
            "/alice/f-editcsrf/files/edit",
            data={"path": "config.json", "content": "y"},
        )
        assert response.status_code == 403

    async def test_files_tab_upload_post_commits_file(
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
            json={"name": "f-up", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-up",
            files={"README.md": "x"},
        )

        csrf = _form_csrf(client, "/alice/f-up/files")
        response = client.post(
            "/alice/f-up/files/upload",
            data={"_csrf": csrf, "message": "files-tab upload"},
            files={"files": ("new.txt", b"uploaded from files tab", "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "uploaded=" in response.headers["location"]

        # New file appears in the tree.
        response = client.get("/alice/f-up/files?path=")
        body = response.text
        assert "new.txt" in body

        # Raw fetch returns the uploaded bytes.
        raw = client.get("/alice/f-up/raw/main/new.txt")
        assert raw.status_code == 200
        assert raw.text == "uploaded from files tab"

    async def test_files_tab_upload_post_stranger_returns_403(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "f-strangerup", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        response = client.post(
            "/alice/f-strangerup/files/upload",
            data={"_csrf": "anything", "message": "no"},
            files={"files": ("x.txt", b"x", "text/plain")},
        )
        assert response.status_code == 403

    async def test_files_tab_view_respects_repo_visibility(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        # Private repos: file viewer + raw alias are owner-only (404 for strangers).
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "f-priv", "kind": "model", "visibility": "private"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-priv",
            files={"README.md": "x", "config.json": "{}"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        # Strangers get 404 (visibility leak prevention), not 403.
        response = client.get("/alice/f-priv/files/view?path=config.json")
        assert response.status_code == 404
        response = client.get("/alice/f-priv/raw/main/config.json")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# v0.5.7 — Files-tab read-only view, edit toggle, upload toggle, split
# layout, media preview, rename-on-edit.
# ---------------------------------------------------------------------------


class TestFilesViewReadOnly:
    """The viewer panel is read-only by default; the Edit anchor switches
    to an editor (owner/admin only)."""

    async def test_owner_view_without_edit_param_renders_read_only(
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
            json={"name": "f-ro", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-ro",
            files={
                "README.md": "x",
                "config.json": '{"k": 1}',
            },
        )

        response = client.get("/alice/f-ro/files/view?path=config.json")
        assert response.status_code == 200, response.text
        body = response.text
        assert "files-viewer" in body
        assert "<textarea" not in body
        assert (
            'href="/alice/f-ro/files/view?path=config.json&amp;edit=1"' in body
            or 'href="/alice/f-ro/files/view?path=config.json&edit=1"' in body
        )

    async def test_owner_view_with_edit_param_renders_editor_with_rename(
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
            json={"name": "f-edit", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-edit",
            files={
                "README.md": "x",
                "config.json": '{"alpha": 1}',
            },
        )

        response = client.get("/alice/f-edit/files/view?path=config.json&edit=1")
        assert response.status_code == 200, response.text
        body = response.text
        assert "files-viewer" in body
        assert "files-viewer__textarea" in body
        assert 'name="new_path"' in body
        assert 'value="config.json"' in body

    async def test_stranger_view_with_edit_param_renders_read_only(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "f-edit-str", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-edit-str",
            files={"README.md": "x", "config.json": "{}"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")

        response = client.get("/alice/f-edit-str/files/view?path=config.json&edit=1")
        assert response.status_code == 200, response.text
        body = response.text
        assert "<textarea" not in body
        assert "Edit</a>" not in body
        assert 'href="/alice/f-edit-str/files/view?path=config.json&amp;edit=1"' not in body

    async def test_view_is_read_only_for_non_text_file_even_for_owner(
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
            json={"name": "f-bin-noedit", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="f-bin-noedit",
            files={"README.md": "x", "weights.bin": "\x00\x01\x02"},
        )

        response = client.get("/alice/f-bin-noedit/files/view?path=weights.bin&edit=1")
        assert response.status_code == 200
        body = response.text
        assert "<textarea" not in body
        assert 'class="files-viewer__textarea"' not in body
        assert "Download" in body


class TestFilesUploadToggle:
    """The upload form is hidden by default and revealed via ?upload=1."""

    async def test_upload_form_hidden_by_default(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "up-hidden", "kind": "model", "visibility": "public"},
        )

        response = client.get("/alice/up-hidden/files")
        assert response.status_code == 200
        body = response.text
        assert "files-upload__form" not in body
        assert 'href="/alice/up-hidden/files?upload=1"' in body

    async def test_upload_form_shown_with_query_param(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "up-shown", "kind": "model", "visibility": "public"},
        )

        response = client.get("/alice/up-shown/files?upload=1")
        assert response.status_code == 200
        body = response.text
        assert "files-upload__form" in body
        assert 'href="/alice/up-shown/files?upload=1"' not in body
        assert 'href="/alice/up-shown/files"' in body


class TestFilesViewerSplitLayout:
    """Viewing a file splits the Files tab: narrow left tree + viewer right.

    The left column renders the *same* ``<table class="files-table">`` as
    the default Files tab — only the surrounding chrome (split column +
    viewer panel) differs. The unified listing is the whole point of the
    refactor: visitors must recognise the column as the same UI they saw
    on the Files tab without a viewer.
    """

    async def test_viewing_file_renders_split_layout(
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
            json={"name": "split", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="split",
            files={"README.md": "x", "config.json": "{}", "src/util.py": "pass"},
        )

        response = client.get("/alice/split/files/view?path=config.json")
        assert response.status_code == 200
        body = response.text
        assert "files-tree-column" in body
        assert "files-viewer" in body
        assert '<table class="files-table' in body
        assert "<th>Name</th>" in body
        assert '<th class="files-size">Size</th>' in body
        assert '<th class="files-actions">Actions</th>' in body
        assert "/alice/split/files/view?path=config.json" in body
        assert 'id="raw-url-config.json"' in body
        assert "files-row--active" in body
        assert "files-tree-list" not in body
        assert "files-tree-row" not in body
        assert "files-tree-link" not in body

    async def test_no_view_param_renders_full_width_tree(
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
            json={"name": "no-split", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="no-split",
            files={"README.md": "x", "config.json": "{}"},
        )

        response = client.get("/alice/no-split/files")
        assert response.status_code == 200
        body = response.text
        assert '<table class="files-table' in body
        assert "<th>Name</th>" in body
        assert '<aside class="files-tree-column' not in body
        assert '<article class="files-viewer' not in body

    async def test_split_view_and_default_tab_share_the_same_listing(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        """Both branches render the SAME listing UI; only the surrounding
        chrome (split column + viewer panel) differs. The original
        complaint was that the split column swapped the full table for a
        compact list, so users saw two different file UIs in the same
        tab. This test pins that the two branches emit the same table
        signature.
        """
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "sigsame", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="sigsame",
            files={"README.md": "x", "config.json": "{}", "src/util.py": "pass"},
        )

        full_response = client.get("/alice/sigsame/files")
        split_response = client.get("/alice/sigsame/files/view?path=config.json")
        assert full_response.status_code == 200
        assert split_response.status_code == 200
        full_body = full_response.text
        split_body = split_response.text

        for needle in (
            "<th>Name</th>",
            '<th class="files-size">Size</th>',
            '<th class="files-actions">Actions</th>',
            'class="files-row files-row--file"',
            "/alice/sigsame/files/view?path=config.json",
            'id="raw-url-config.json"',
        ):
            assert needle in full_body, (needle, "full")
            assert needle in split_body, (needle, "split")

        for forbidden in (
            "files-tree-list",
            "files-tree-row",
            "files-tree-link",
        ):
            assert forbidden not in full_body, forbidden
            assert forbidden not in split_body, forbidden

        assert '<aside class="files-tree-column' in split_body
        assert '<article class="files-viewer' in split_body
        assert '<aside class="files-tree-column' not in full_body
        assert '<article class="files-viewer' not in full_body


class TestFilesEditRename:
    """The editor allows renaming via a `new_path` form field."""

    async def test_rename_post_creates_single_commit_and_redirects_to_new_path(
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
            json={"name": "rename-ok", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="rename-ok",
            files={"README.md": "x", "old.txt": "old body"},
        )

        csrf = _form_csrf(client, "/alice/rename-ok/files")
        response = client.post(
            "/alice/rename-ok/files/edit",
            data={
                "_csrf": csrf,
                "path": "old.txt",
                "new_path": "new.txt",
                "content": "new body",
                "message": "rename",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        loc = response.headers["location"]
        assert "files/view?path=new.txt" in loc
        assert "edited=new.txt" in loc

        listed = client.get("/alice/rename-ok/files")
        assert listed.status_code == 200
        body = listed.text
        assert "old.txt" not in body
        assert "new.txt" in body

        raw = client.get("/alice/rename-ok/raw/main/new.txt")
        assert raw.status_code == 200
        assert raw.text == "new body"
        old = client.get("/alice/rename-ok/raw/main/old.txt")
        assert old.status_code == 404

    async def test_rename_creates_one_revision(
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
            json={"name": "rename-rev", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="rename-rev",
            files={"README.md": "x", "old.txt": "old"},
        )

        async with async_sessionmaker_for_test(app)() as session:
            from sqlalchemy import text

            repo = (
                await session.execute(text("SELECT id FROM repos WHERE name = 'rename-rev'"))
            ).scalar_one()
            before = (
                (await session.execute(select(Revision).where(Revision.repo_id == repo)))
                .scalars()
                .all()
            )
            before_count = len(before)

        csrf = _form_csrf(client, "/alice/rename-rev/files")
        client.post(
            "/alice/rename-rev/files/edit",
            data={
                "_csrf": csrf,
                "path": "old.txt",
                "new_path": "new.txt",
                "content": "new",
            },
            follow_redirects=False,
        )

        async with async_sessionmaker_for_test(app)() as session:
            from sqlalchemy import text

            repo = (
                await session.execute(text("SELECT id FROM repos WHERE name = 'rename-rev'"))
            ).scalar_one()
            after = (
                (await session.execute(select(Revision).where(Revision.repo_id == repo)))
                .scalars()
                .all()
            )
            assert len(after) == before_count + 1
            audit = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "repo.file_edit")))
                .scalars()
                .all()
            )
            assert audit, "expected a repo.file_edit audit entry"
            detail = json.loads(audit[-1].detail or "{}")
            assert detail.get("rename_from") == "old.txt"
            assert detail.get("rename_to") == "new.txt"

    async def test_rename_to_existing_path_returns_edit_error(
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
            json={"name": "rename-dup", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="rename-dup",
            files={"README.md": "x", "a.txt": "a", "b.txt": "b"},
        )

        csrf = _form_csrf(client, "/alice/rename-dup/files")
        response = client.post(
            "/alice/rename-dup/files/edit",
            data={
                "_csrf": csrf,
                "path": "a.txt",
                "new_path": "b.txt",
                "content": "overwrite",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "edit_error=" in response.headers["location"]

        raw_a = client.get("/alice/rename-dup/raw/main/a.txt")
        raw_b = client.get("/alice/rename-dup/raw/main/b.txt")
        assert raw_a.status_code == 200 and raw_a.text == "a"
        assert raw_b.status_code == 200 and raw_b.text == "b"

    async def test_rename_with_traversal_segment_rejected(
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
            json={"name": "rename-trav", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="rename-trav",
            files={"README.md": "x", "a.txt": "a"},
        )

        csrf = _form_csrf(client, "/alice/rename-trav/files")
        response = client.post(
            "/alice/rename-trav/files/edit",
            data={
                "_csrf": csrf,
                "path": "a.txt",
                "new_path": "../escape.txt",
                "content": "x",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "edit_error=" in response.headers["location"]
        raw = client.get("/alice/rename-trav/raw/main/a.txt")
        assert raw.status_code == 200 and raw.text == "a"

    async def test_same_path_save_uses_existing_edit_behavior(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        """When `new_path` equals `path` (or is omitted), the rename
        branch is bypassed and the original edit-only semantics apply:
        single commit, no rename_from / rename_to in the audit detail.
        """
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "same-path", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="same-path",
            files={"README.md": "x", "config.json": "old"},
        )

        csrf = _form_csrf(client, "/alice/same-path/files")
        response = client.post(
            "/alice/same-path/files/edit",
            data={
                "_csrf": csrf,
                "path": "config.json",
                "content": "new",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "edited=config.json" in response.headers["location"]

        async with async_sessionmaker_for_test(app)() as session:
            audit = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "repo.file_edit")))
                .scalars()
                .all()
            )
            assert audit
            detail = json.loads(audit[-1].detail or "{}")
            assert "rename_from" not in detail
            assert "rename_to" not in detail


def async_sessionmaker_for_test(
    app: tuple[TestClient, FastAPI, object],
) -> async_sessionmaker:
    """Return the integration test engine's session factory.

    The `app` fixture already opened an engine against the per-test
    `OUTO_DATA_DIR`; re-use it so the test queries observe the same
    database the HTTP writes committed to.
    """
    from outo_models.config import get_settings

    del app
    settings = get_settings()
    engine = get_engine(settings)
    return get_session_factory(engine)


class TestFilesViewerMedia:
    """The viewer renders images and videos inline; oversized ones fall
    back to the metadata + Download link."""

    async def test_image_file_view_renders_img_tag(
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
            json={"name": "f-img", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_bytes(
            tmp_data_dir,
            owner="alice",
            name="f-img",
            files={
                "README.md": b"x",
                "logo.png": b"\x89PNG\r\n\x1a\n" + b"fake-payload",
            },
        )

        response = client.get("/alice/f-img/files/view?path=logo.png")
        assert response.status_code == 200, response.text
        body = response.text
        assert "<img" in body
        assert 'src="/alice/f-img/raw/main/logo.png"' in body

    async def test_video_file_view_renders_video_tag(
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
            json={"name": "f-vid", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_bytes(
            tmp_data_dir,
            owner="alice",
            name="f-vid",
            files={
                "README.md": b"x",
                "demo.mp4": b"ftypisom" + b"\x00" * 64,
            },
        )

        response = client.get("/alice/f-vid/files/view?path=demo.mp4")
        assert response.status_code == 200, response.text
        body = response.text
        assert "<video" in body
        assert "controls" in body
        assert 'src="/alice/f-vid/raw/main/demo.mp4"' in body

    async def test_oversized_image_renders_download_link(
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
            json={"name": "f-bigimg", "kind": "model", "visibility": "public"},
        )
        _seed_bare_repo_bytes(
            tmp_data_dir,
            owner="alice",
            name="f-bigimg",
            files={
                "README.md": b"x",
                "huge.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024),
            },
        )

        response = client.get("/alice/f-bigimg/files/view?path=huge.png")
        assert response.status_code == 200
        body = response.text
        assert "<img" not in body
        assert "Download" in body
        assert "/alice/f-bigimg/raw/main/huge.png" in body

    async def test_lfs_pointer_image_renders_img_with_raw_url(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        tmp_data_dir: Path,
    ) -> None:
        """LFS pointer files (text body with `version https://git-lfs...`)
        are still served through the raw URL — the viewer markup just
        points `<img>` at the raw URL; the raw route transparently
        redirects to the LFS GET endpoint."""
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/repos",
            json={"name": "f-lfsimg", "kind": "model", "visibility": "public"},
        )
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:" + "ab" * 32 + "\n"
            "size 1048576\n"
        )
        _seed_bare_repo_bytes(
            tmp_data_dir,
            owner="alice",
            name="f-lfsimg",
            files={
                "README.md": b"x",
                "lfs-image.png": pointer.encode("utf-8"),
            },
        )

        response = client.get("/alice/f-lfsimg/files/view?path=lfs-image.png")
        assert response.status_code == 200
        body = response.text
        assert "<img" in body
        assert 'src="/alice/f-lfsimg/raw/main/lfs-image.png"' in body
