"""Integration tests for the site-wide Posts feature.

Covers:
    * `GET /posts` feed renders newest-first with reaction totals.
    * `GET /posts/new` redirects anonymous users to /login; renders the
      composer for authenticated users (login + CSRF).
    * `POST /posts/new` creates short + long posts; validation:
        - long without a title → re-render with error
        - short > 2000 chars → re-render with error
        - bad repo link → re-render with error
        - anonymous → 303 redirect to /login?next=/posts/new
    * `GET /posts/{id}` renders the detail page with sanitized markdown
      (a `<script>` injection in a long body is stripped).
    * `POST /posts/{id}/react` toggles a reaction (add then remove);
      a second user's reaction is counted independently. Bad emoji → 422.
    * `POST /posts/{id}/comments` adds a comment; deletion is author- or
      admin-only.
    * `POST /posts/{id}/delete` is author- or admin-only (stranger → 403).
    * The profile page (`/{username}`) lists the user's posts newest-first.
    * Repo visibility: a private-repo-linked post shows the chip only
      to viewers who can see the repo (owner / admin); the post itself
      remains readable, with the chip hidden.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.db import Post, PostComment, PostReaction, Repo, User

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered
# from tests/integration/conftest.py.


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
    return match.group(1)


async def _create_repo(
    factory: async_sessionmaker,
    *,
    owner: User,
    name: str,
    kind: str = "model",
    visibility: str = "public",
) -> Repo:
    """Create a Repo row via direct DB writes (skips the HTTP API)."""
    async with factory() as session:
        repo = Repo(
            owner_id=owner.id,
            name=name,
            kind=kind,
            visibility=visibility,
            description=f"{name} description",
            default_branch="main",
            size_bytes=0,
            path=f"{owner.username}/{name}",
        )
        session.add(repo)
        await session.commit()
        repo_id = repo.id
    # Re-read in a fresh session so the returned row carries the
    # eagerly-loaded owner relationship (consumers expect it).
    async with factory() as session:
        from sqlalchemy.orm import selectinload

        return (
            await session.execute(
                select(Repo).where(Repo.id == repo_id).options(selectinload(Repo.owner))
            )
        ).scalar_one()


async def _create_post(
    factory: async_sessionmaker,
    *,
    author: User,
    kind: str = "short",
    body: str = "hello world",
    title: str | None = None,
    repo: Repo | None = None,
) -> int:
    """Create a Post row directly via DB writes; return the post id."""
    async with factory() as session:
        post = Post(
            author_id=author.id,
            kind=kind,
            title=title,
            body=body,
            repo_id=repo.id if repo is not None else None,
        )
        session.add(post)
        await session.commit()
        return int(post.id)


# ---------------------------------------------------------------------------
# /posts feed
# ---------------------------------------------------------------------------


class TestPostsFeed:
    """`GET /posts` shows the global feed."""

    async def test_anonymous_can_view_feed(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/posts")
        assert response.status_code == 200
        body = response.text
        assert "Posts" in body
        # Empty-state hint present (no posts yet).
        assert "No posts yet" in body

    async def test_feed_lists_short_and_long_posts(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            short_id = await _create_post(factory, author=alice, kind="short", body="first short")
            long_id = await _create_post(
                factory,
                author=alice,
                kind="long",
                title="Long post",
                body="long body",
            )

        response = client.get("/posts")
        assert response.status_code == 200
        body = response.text
        # Both posts appear; short is rendered as escaped text + br;
        # long renders the title + sanitized markdown.
        assert "first short" in body
        assert "long body" in body
        assert "Long post" in body
        # The post id URLs appear in the read-more links.
        assert f"/posts/{short_id}" in body
        assert f"/posts/{long_id}" in body

    async def test_feed_shows_excerpt_not_full_long_body(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            head = "excerpt-head-paragraph " + "x" * 120
            tail = "tail-paragraph-beyond-the-cap " + "y" * 400
            post_id = await _create_post(
                factory,
                author=alice,
                kind="long",
                title="Excerpted",
                body=f"{head}\n\n{tail}",
            )

        response = client.get("/posts")
        assert response.status_code == 200
        body = response.text
        assert "excerpt-head-paragraph" in body
        assert "tail-paragraph-beyond-the-cap" not in body
        assert "Read more" in body

        detail = client.get(f"/posts/{post_id}")
        assert detail.status_code == 200
        assert "tail-paragraph-beyond-the-cap" in detail.text

    async def test_feed_and_detail_css_prevent_horizontal_overflow(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_post(factory, author=alice, body="css probe")

        feed = client.get("/posts").text
        for marker in (
            "overflow-wrap: anywhere",
            "minmax(0, 1fr)",
            "white-space: pre-wrap",
        ):
            assert marker in feed

        detail = client.get("/posts/1").text
        assert "overflow-wrap: anywhere" in detail
        assert "minmax(0, 1fr)" in detail

    async def test_modals_close_via_scripted_close_buttons(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_post(factory, author=alice, body="close probe")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )

        feed = client.get("/posts").text
        assert 'src="/static/posts.js"' in feed
        assert feed.count('data-modal-close aria-label="Close"') == 3
        assert '<details class="post-comments-modal" data-modal' in feed
        assert '<details class="post-picker" data-modal>' in feed
        assert '<details class="post-new-modal" data-modal' in feed
        assert 'href="/posts#post-' not in feed

        detail = client.get("/posts/1").text
        assert 'src="/static/posts.js"' in detail
        assert detail.count('data-modal-close aria-label="Close"') == 1

    async def test_feed_orders_newest_first(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_post(factory, author=alice, body="older post")
            await _create_post(factory, author=alice, body="newer post")

        response = client.get("/posts")
        body = response.text
        # Newest must appear before older in the HTML.
        newer_pos = body.find("newer post")
        older_pos = body.find("older post")
        assert newer_pos != -1 and older_pos != -1
        assert newer_pos < older_pos

    async def test_feed_renders_reaction_totals_and_viewer_state(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        bob_id = await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            bob = (await session.execute(select(User).where(User.id == bob_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="popular post")
            session.add(PostReaction(post_id=post_id, user_id=alice.id, emoji="\U0001f44d"))
            session.add(PostReaction(post_id=post_id, user_id=bob.id, emoji="\U0001f44d"))
            await session.commit()

        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.get("/posts")
        body = response.text
        assert "\U0001f44d" in body
        assert 'class="post-tile__chip-count">2</span>' in body
        assert "post-tile__chip--active" in body
        assert 'class="post-picker"' in body
        assert "post-picker__btn" in body

    async def test_feed_no_full_emoji_button_row(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        """The 26-emoji *button row* is gone — only the picker (inside
        its <details> modal) and the *applied* reaction chips remain."""
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/posts")
        body = response.text
        # The pre-v0.6.2 button-row class is retired.
        assert "post-tile__reaction--active" not in body
        assert "post-tile__chip" in body

    async def test_feed_renders_block_link_to_detail(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(
                factory, author=alice, kind="long", title="Linked title", body="body"
            )
        response = client.get("/posts")
        body = response.text
        assert f'<a class="post-tile__link" href="/posts/{post_id}">' in body

    async def test_feed_comments_modal_present(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="discuss")
            session.add(PostComment(post_id=post_id, author_id=alice.id, body="inline comment"))
            await session.commit()
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/posts")
        body = response.text
        assert f'id="post-{post_id}-comments"' in body
        assert "inline comment" in body
        assert f'action="/posts/{post_id}/comments"' in body
        assert 'name="next" value="feed"' in body

    async def test_feed_comments_modal_opens_via_query_string(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        """`GET /posts?comments=<id>` opens the comments modal for that post."""
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get(f"/posts?comments={post_id}")
        body = response.text
        # Jinja2 preserves whitespace between attributes, so the open
        # marker sits on its own line — regex anchors the contract.
        pattern = (
            r'<details\s+class="post-comments-modal"\s+data-modal\s+'
            r'id="post-' + str(post_id) + r'-comments"\s+open>'
        )
        assert re.search(pattern, body) is not None

    async def test_feed_new_post_modal_renders_for_logged_in_user(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/posts")
        body = response.text
        # Class-name presence — the surrounding `<details ... >` keeps
        # an internal space when the optional `open` attribute is absent.
        assert re.search(r'<details\s+class="post-new-modal"', body) is not None
        assert 'name="kind" value="short"' in body
        assert 'href="/posts/new"' in body

    async def test_feed_container_is_900px(
        self,
        app: tuple[TestClient, FastAPI, object],
    ) -> None:
        """The feed's content column is capped at 900px (디자인.md cap)."""
        client, _, _ = app
        response = client.get("/posts")
        assert response.status_code == 200
        assert "max-width: 900px" in response.text


# ---------------------------------------------------------------------------
# /posts/new (composer)
# ---------------------------------------------------------------------------


class TestPostsNew:
    """`GET /posts/new` + `POST /posts/new`."""

    async def test_anon_get_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/posts/new", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert "next=/posts/new" in response.headers["location"]

    async def test_anon_post_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.post(
            "/posts/new",
            data={"kind": "short", "body": "hi", "title": "", "repo_link": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert "next=/posts/new" in response.headers["location"]

    async def test_post_without_csrf_is_rejected(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/posts/new",
            data={"kind": "short", "body": "no csrf"},
        )
        assert response.status_code == 403

    async def test_create_short_post(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "hello short world",
                "repo_link": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        # Redirect lands on the detail page.
        location = response.headers["location"]
        assert location.startswith("/posts/")
        # The DB row exists with the expected shape.
        async with factory() as session:
            post = (
                await session.execute(select(Post).where(Post.body == "hello short world"))
            ).scalar_one()
            assert post.kind == "short"
            assert post.title is None
            assert post.author_id is not None

    async def test_create_long_post_with_title(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "long",
                "title": "My Paper",
                "body": "# Heading\n\nBody text.",
                "repo_link": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        async with factory() as session:
            post = (
                await session.execute(select(Post).where(Post.title == "My Paper"))
            ).scalar_one()
            assert post.kind == "long"
            assert post.title == "My Paper"

    async def test_long_without_title_rerenders_with_error(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "long",
                "title": "",
                "body": "long body without a title",
                "repo_link": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "errors" in response.text
        # The typed values are preserved on re-render.
        assert 'value=""' in response.text  # the empty title

    async def test_short_over_2000_chars_rerenders_with_error(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "x" * 2001,
                "repo_link": "",
            },
            follow_redirects=False,
        )
        # Short-post errors stay in the feed context — the modal
        # reopens server-side via `?post_modal=new`.
        assert response.status_code == 303
        assert response.headers["location"].startswith("/posts?")
        assert "post_modal=new" in response.headers["location"]
        followed = client.get(response.headers["location"])
        assert followed.status_code == 200
        assert "errors" in followed.text
        assert "2000" in followed.text
        # The new-post <details> carries `open` so the modal stays
        # visible after the redirect lands.
        new_modal_open = re.search(
            r'<details\s+class="post-new-modal"\s+data-modal(\s+open)?>', followed.text
        )
        assert new_modal_open is not None

    async def test_bad_repo_link_rerenders_with_error(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "link me",
                "repo_link": "noslashhere",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "post_modal=new" in response.headers["location"]
        followed = client.get(response.headers["location"])
        assert followed.status_code == 200
        assert "errors" in followed.text
        assert "owner/name" in followed.text

    async def test_post_with_repo_link_persists_repo_id(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_repo(factory, owner=alice, name="linked-repo")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "linked post",
                "repo_link": "alice/linked-repo",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        async with factory() as session:
            post = (
                await session.execute(select(Post).where(Post.body == "linked post"))
            ).scalar_one()
            assert post.repo_id is not None

    async def test_post_linking_private_repo_rejected(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_repo(factory, owner=alice, name="private-repo", visibility="private")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        response = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "should fail",
                "repo_link": "alice/private-repo",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        followed = client.get(response.headers["location"])
        assert followed.status_code == 200
        assert "errors" in followed.text
        assert "not found" in followed.text.lower()


# ---------------------------------------------------------------------------
# Detail page + sanitized markdown
# ---------------------------------------------------------------------------


class TestPostsDetail:
    """`GET /posts/{id}` renders sanitized body + reactions + comments."""

    async def test_detail_renders_short_body_escaped(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(
                factory,
                author=alice,
                body="line1\nline2\n<script>alert(1)</script>",
            )

        response = client.get(f"/posts/{post_id}")
        assert response.status_code == 200
        body = response.text
        # Short body: <script> is escaped, line breaks become <br>.
        assert "<script>alert" not in body
        assert "&lt;script&gt;alert" in body
        # Newline preserved as <br>.
        assert "line1<br>line2" in body

    async def test_detail_renders_long_body_sanitized(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(
                factory,
                author=alice,
                kind="long",
                title="Sanitization",
                body="# Heading\n\n<script>alert('xss')</script>\n\nBody text.",
            )

        response = client.get(f"/posts/{post_id}")
        body = response.text
        # Sanitizer strips the <script> tag (the body_html emitted via
        # card.py's _SANITIZE_RE).
        assert "<script>" not in body
        assert "alert" not in body
        # Title + markdown heading render.
        assert "Sanitization" in body
        assert "Heading" in body

    async def test_detail_private_repo_chip_hidden_for_stranger(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            repo = await _create_repo(factory, owner=alice, name="priv", visibility="private")
            post_id = await _create_post(factory, author=alice, body="about private", repo=repo)

        # Bob (stranger) loads the detail page — chip must be hidden.
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.get(f"/posts/{post_id}")
        assert response.status_code == 200
        body = response.text
        assert "alice/priv" not in body

        # Owner alice sees the chip.
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        owner_response = client.get(f"/posts/{post_id}")
        assert "alice/priv" in owner_response.text


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestPostReactions:
    """`POST /posts/{id}/react` toggles a reaction per (post, user, emoji)."""

    async def test_anon_react_redirects_to_login(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="r")

        response = client.post(
            f"/posts/{post_id}/react",
            data={"_csrf": "any", "emoji": "\U0001f44d"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    async def test_react_toggle_adds_then_removes(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        # Need a post to react on. Create one via the API path so the
        # client-side cookie flow stays simple.
        csrf = _form_csrf(client, "/posts/new")
        create = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "react target",
                "repo_link": "",
            },
            follow_redirects=False,
        )
        assert create.status_code == 303
        post_id = int(create.headers["location"].rsplit("/", 1)[-1])

        # Toggle on.
        csrf = _form_csrf(client, f"/posts/{post_id}")
        client.post(
            f"/posts/{post_id}/react",
            data={"_csrf": csrf, "emoji": "\U0001f44d"},
            follow_redirects=False,
        )
        async with factory() as session:
            reactions = (
                (await session.execute(select(PostReaction).where(PostReaction.post_id == post_id)))
                .scalars()
                .all()
            )
        assert len(reactions) == 1
        assert reactions[0].emoji == "\U0001f44d"

        # Toggle off.
        csrf = _form_csrf(client, f"/posts/{post_id}")
        client.post(
            f"/posts/{post_id}/react",
            data={"_csrf": csrf, "emoji": "\U0001f44d"},
            follow_redirects=False,
        )
        async with factory() as session:
            reactions = (
                (await session.execute(select(PostReaction).where(PostReaction.post_id == post_id)))
                .scalars()
                .all()
            )
        assert len(reactions) == 0

    async def test_react_accepts_v061_palette_glyphs(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/new")
        create = client.post(
            "/posts/new",
            data={
                "_csrf": csrf,
                "kind": "short",
                "title": "",
                "body": "palette target",
                "repo_link": "",
            },
            follow_redirects=False,
        )
        assert create.status_code == 303
        post_id = int(create.headers["location"].rsplit("/", 1)[-1])

        csrf = _form_csrf(client, f"/posts/{post_id}")
        for glyph in ("\U0001faea", "\u203c\ufe0f", "\U0001f344", "\U0001f31d"):
            response = client.post(
                f"/posts/{post_id}/react",
                data={"_csrf": csrf, "emoji": glyph},
                follow_redirects=False,
            )
            assert response.status_code == 303

        async with factory() as session:
            reactions = (
                (await session.execute(select(PostReaction).where(PostReaction.post_id == post_id)))
                .scalars()
                .all()
            )
        assert {r.emoji for r in reactions} == {
            "\U0001faea",
            "\u203c\ufe0f",
            "\U0001f344",
            "\U0001f31d",
        }

    async def test_react_two_users_aggregate(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        bob_id = await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            bob = (await session.execute(select(User).where(User.id == bob_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="agg")
            session.add(PostReaction(post_id=post_id, user_id=alice.id, emoji="\U0001f44d"))
            session.add(PostReaction(post_id=post_id, user_id=bob.id, emoji="\U0001f44d"))
            await session.commit()

        detail = client.get(f"/posts/{post_id}")
        body = detail.text
        assert "\U0001f44d" in body
        # The aggregate count "2" is rendered after the glyph: `<emoji> 2`.
        assert "\U0001f44d 2" in body

    async def test_react_invalid_emoji_is_rejected(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/react",
            data={"_csrf": csrf, "emoji": "🦄"},
            follow_redirects=False,
        )
        # Bad emoji is a validation error — the route still 303s back
        # to the detail page, but no row was written.
        assert response.status_code == 303
        async with factory() as session:
            reactions = (
                (await session.execute(select(PostReaction).where(PostReaction.post_id == post_id)))
                .scalars()
                .all()
            )
            assert len(reactions) == 0


# ---------------------------------------------------------------------------
# Comments + delete
# ---------------------------------------------------------------------------


class TestPostComments:
    """`POST /posts/{id}/comments` adds a flat comment."""

    async def test_add_comment_and_render(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        bob_id = await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="discuss")

        # Bob comments on alice's post.
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/comments",
            data={"_csrf": csrf, "body": "great post"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        # Detail page renders the comment.
        detail = client.get(f"/posts/{post_id}")
        assert "great post" in detail.text
        assert "bob" in detail.text

        # DB row exists with the correct shape.
        async with factory() as session:
            comment = (
                await session.execute(select(PostComment).where(PostComment.post_id == post_id))
            ).scalar_one()
            assert comment.body == "great post"
            assert comment.author_id == bob_id

    async def test_comment_blank_body_is_ignored(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/comments",
            data={"_csrf": csrf, "body": "   "},
            follow_redirects=False,
        )
        assert response.status_code == 303
        async with factory() as session:
            assert (
                await session.execute(select(PostComment).where(PostComment.post_id == post_id))
            ).scalars().all() == []

    async def test_comment_delete_authorization(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            comment_id: int
            async with factory() as session:
                post_id = await _create_post(factory, author=alice, body="x")
                session.add(PostComment(post_id=post_id, author_id=alice_id, body="hi from alice"))
                await session.commit()
                comment_id = (
                    (
                        await session.execute(
                            select(PostComment).where(PostComment.post_id == post_id)
                        )
                    )
                    .scalars()
                    .one()
                    .id
                )

        # Alice (comment author) can delete.
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/comments/{comment_id}/delete",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        async with factory() as session:
            assert (
                await session.execute(select(PostComment).where(PostComment.id == comment_id))
            ).scalar_one_or_none() is None


class TestPostDelete:
    """`POST /posts/{id}/delete` is author- or admin-only."""

    async def test_author_can_delete(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/delete",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/posts"
        async with factory() as session:
            assert (
                await session.execute(select(Post).where(Post.id == post_id))
            ).scalar_one_or_none() is None

    async def test_stranger_cannot_delete(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/delete",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        # Stranger cannot delete — domain layer raises Forbidden.
        assert response.status_code == 303
        async with factory() as session:
            assert (
                await session.execute(select(Post).where(Post.id == post_id))
            ).scalar_one_or_none() is not None

    async def test_admin_can_delete(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        await seed_approved_user(username="root", role="admin")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "root", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/delete",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        async with factory() as session:
            assert (
                await session.execute(select(Post).where(Post.id == post_id))
            ).scalar_one_or_none() is None

    async def test_delete_cascades_reactions_and_comments(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        bob_id = await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            bob = (await session.execute(select(User).where(User.id == bob_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
            session.add(PostReaction(post_id=post_id, user_id=bob.id, emoji="\U0001f44d"))
            session.add(PostComment(post_id=post_id, author_id=bob.id, body="hi"))
            await session.commit()

        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        client.post(
            f"/posts/{post_id}/delete",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        async with factory() as session:
            assert (
                await session.execute(select(PostReaction).where(PostReaction.post_id == post_id))
            ).scalars().all() == []
            assert (
                await session.execute(select(PostComment).where(PostComment.post_id == post_id))
            ).scalars().all() == []


# ---------------------------------------------------------------------------
# Navbar + profile section
# ---------------------------------------------------------------------------


class TestNavAndProfile:
    """The Posts nav link is present, active on /posts, and the user's
    profile page lists their posts."""

    async def test_nav_has_posts_link(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/")
        body = response.text
        assert 'href="/posts"' in body
        # Nav ordering: Spaces < Posts (Posts comes after Spaces).
        nav = body.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        spaces = nav.find('href="/spaces"')
        posts = nav.find('href="/posts"')
        assert spaces != -1 and posts != -1
        assert spaces < posts, "Posts link must follow Spaces"

    async def test_nav_active_state_on_feed(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/posts")
        body = response.text
        nav = body.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/posts" class="nav-link active"' in nav
        # The other kind links stay inactive.
        assert 'href="/models" class="nav-link active"' not in nav
        assert 'href="/datasets" class="nav-link active"' not in nav
        assert 'href="/spaces" class="nav-link active"' not in nav

    async def test_profile_lists_user_posts(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_post(factory, author=alice, body="first alice post")
            await _create_post(factory, author=alice, body="second alice post")
        response = client.get("/alice")
        assert response.status_code == 200
        body = response.text
        assert "first alice post" in body
        assert "second alice post" in body
        # Section heading + tile count in the nav tab.
        assert "Posts" in body
        assert "(2)" in body

    async def test_profile_empty_posts_section(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        response = client.get("/alice")
        body = response.text
        assert "hasn't posted yet" in body or "haven't posted yet" in body


# ---------------------------------------------------------------------------
# Private-repo chip privacy on the feed
# ---------------------------------------------------------------------------


class TestFeedRepoChipPrivacy:
    """Linked-repo chips on the feed follow repo visibility."""

    async def test_public_repo_chip_visible_to_stranger(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            repo = await _create_repo(factory, owner=alice, name="pub")
            await _create_post(factory, author=alice, body="public-repo mention", repo=repo)

        response = client.get("/posts")
        assert "alice/pub" in response.text

    async def test_private_repo_chip_hidden_from_stranger_on_feed(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            repo = await _create_repo(factory, owner=alice, name="hidden", visibility="private")
            await _create_post(factory, author=alice, body="private-repo mention", repo=repo)

        response = client.get("/posts")
        body = response.text
        # The post body is still visible.
        assert "private-repo mention" in body
        # The chip is hidden so the private repo name is not leaked.
        assert "alice/hidden" not in body


# ---------------------------------------------------------------------------
# Long-form editor + /posts/preview (server-side markdown preview)
# ---------------------------------------------------------------------------


class TestPostsPreview:
    """`GET /posts/preview` + `POST /posts/preview` render the editor and
    a sanitized server-rendered markdown preview."""

    async def test_anon_get_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/posts/preview", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert "next=/posts/preview" in response.headers["location"]

    async def test_anon_post_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.post(
            "/posts/preview",
            data={"_csrf": "x", "title": "x", "body": "x"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    async def test_get_renders_empty_editor(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/posts/preview")
        assert response.status_code == 200
        body = response.text
        # formaction is pure HTML, no JS needed to switch targets.
        assert '<form method="post" action="/posts/preview"' in body
        assert 'formaction="/posts/new"' in body
        assert 'class="post-editor__preview"' not in body

    async def test_post_renders_sanitized_preview(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts/preview")
        response = client.post(
            "/posts/preview",
            data={
                "_csrf": csrf,
                "title": "My Title",
                "body": "# Heading\n\n<script>alert('xss')</script>\n\nBody text.",
            },
        )
        assert response.status_code == 200
        body = response.text
        assert "<script>" not in body
        assert "My Title" in body
        assert "Heading" in body
        assert 'class="post-editor__preview"' in body
        # Draft preservation — Publish reads the same inputs without JS.
        assert 'value="My Title"' in body
        assert "Body text." in body
        # The preview pane (not the textarea draft) is sanitized —
        # the sanitizer strips <script> + `alert` from the rendered
        # markdown only; the textarea keeps the raw draft for editing.
        preview_section = body.split('class="post-editor__preview"', 1)[1].split("</section>", 1)[0]
        assert "<script>" not in preview_section
        assert "alert" not in preview_section

    async def test_post_without_csrf_is_rejected(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/posts/preview",
            data={"title": "x", "body": "y"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Feed modal interactions (comment / reaction / new-post from feed context)
# ---------------------------------------------------------------------------


class TestPostsFeedModals:
    """POST handlers invoked from the feed return the user to `/posts`."""

    async def test_react_from_feed_redirects_back_to_feed(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts")
        response = client.post(
            f"/posts/{post_id}/react",
            data={"_csrf": csrf, "emoji": "\U0001f44d", "next": "feed"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/posts"

    async def test_react_from_detail_keeps_permalink_redirect(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, f"/posts/{post_id}")
        response = client.post(
            f"/posts/{post_id}/react",
            data={"_csrf": csrf, "emoji": "\U0001f44d"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        # Detail-page path keeps the permalink (no `next` token).
        assert response.headers["location"] == f"/posts/{post_id}"

    async def test_comment_from_feed_reopens_modal(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="discuss")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        csrf = _form_csrf(client, "/posts")
        response = client.post(
            f"/posts/{post_id}/comments",
            data={"_csrf": csrf, "body": "feed-context comment", "next": "feed"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/posts?comments={post_id}"
        followed = client.get(response.headers["location"])
        body = followed.text
        assert (
            re.search(
                r'<details\s+class="post-comments-modal"\s+data-modal\s+id="post-'
                + str(post_id)
                + r'-comments"\s+open>',
                body,
            )
            is not None
        )
        assert "feed-context comment" in body

    async def test_feed_modal_picker_contains_all_26_glyphs(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        from outo_models.db.models.posts import REACTION_PALETTE

        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get("/posts")
        body = response.text
        for glyph in REACTION_PALETTE:
            assert f'value="{glyph}"' in body, f"missing glyph: {glyph!r}"
        picker_section = body.split('<details class="post-picker"', 1)[1].split("</details>", 1)[0]
        for glyph in REACTION_PALETTE:
            assert picker_section.count(f">{glyph}</button>") >= 1, (
                f"picker missing glyph: {glyph!r}"
            )


# ---------------------------------------------------------------------------
# Detail page chip + picker migration
# ---------------------------------------------------------------------------


class TestPostsDetailModals:
    """Detail page adopts chips + picker; comments stay inline."""

    async def test_detail_renders_chips_not_full_button_row(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
            session.add(PostReaction(post_id=post_id, user_id=alice.id, emoji="\U0001f44d"))
            await session.commit()
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get(f"/posts/{post_id}")
        body = response.text
        assert "post-detail__chip" in body
        assert "post-picker" in body
        # The pre-v0.6.2 button-row class is retired.
        assert "post-detail__reaction " not in body

    async def test_detail_picker_carries_all_palette_glyphs(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        from outo_models.db.models.posts import REACTION_PALETTE

        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            post_id = await _create_post(factory, author=alice, body="x")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.get(f"/posts/{post_id}")
        body = response.text
        picker_section = body.split('<details class="post-picker"', 1)[1].split("</details>", 1)[0]
        for glyph in REACTION_PALETTE:
            assert picker_section.count(f">{glyph}</button>") >= 1, (
                f"detail picker missing glyph: {glyph!r}"
            )


__all__ = [
    "TestFeedRepoChipPrivacy",
    "TestNavAndProfile",
    "TestPostComments",
    "TestPostDelete",
    "TestPostReactions",
    "TestPostsDetail",
    "TestPostsDetailModals",
    "TestPostsFeed",
    "TestPostsFeedModals",
    "TestPostsNew",
    "TestPostsPreview",
]
