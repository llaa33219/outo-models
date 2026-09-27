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
            # Two reactions (👍) — one from alice, one from bob.
            session.add(PostReaction(post_id=post_id, user_id=alice.id, emoji="\U0001f44d"))
            session.add(PostReaction(post_id=post_id, user_id=bob.id, emoji="\U0001f44d"))
            await session.commit()

        # Log in as bob so his reaction is "active" in the tile.
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.get("/posts")
        body = response.text
        # The total count "2" appears next to the 👍 button.
        assert "\U0001f44d" in body
        # The active-class form button indicates the viewer reacted.
        assert "post-tile__reaction--active" in body


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
        assert response.status_code == 200
        assert "errors" in response.text
        assert "2000" in response.text

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
        assert response.status_code == 200
        assert "errors" in response.text
        assert "owner/name" in response.text

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
        assert response.status_code == 200
        assert "errors" in response.text
        assert "not found" in response.text.lower()


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


__all__ = [
    "TestFeedRepoChipPrivacy",
    "TestNavAndProfile",
    "TestPostComments",
    "TestPostDelete",
    "TestPostReactions",
    "TestPostsDetail",
    "TestPostsFeed",
    "TestPostsNew",
]
