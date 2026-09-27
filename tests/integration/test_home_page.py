"""Integration tests for the home page (`GET /`).

The home page is no longer a single "Public repositories" listing.
It mixes four kinds of tiles:

    * Trending posts (top by reaction count over the last 7 days,
      fallback to most-recent when no reactions exist).
    * Top public models ordered by `downloads_count`.
    * Top public datasets ordered by `downloads_count`.
    * Public Spaces ordered by `updated_at desc` (the documented
      approximation because the Spaces v2 runtime does not persist
      a `running_since` timestamp).

The contract under test:

    * The page contains all four tile kinds as soon as the right
      rows exist in the DB.
    * The page NEVER contains the old "Public repositories" heading.
    * Empty-state messaging renders when the home is empty.
    * Private repos are filtered from the top-models / top-datasets /
      spaces lists (matching the kind catalogs' visibility rules).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.db import Post, PostReaction, Repo, User

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered
# from tests/integration/conftest.py.


async def _seed_repo(
    factory: async_sessionmaker,
    *,
    owner: User,
    name: str,
    kind: str,
    visibility: str = "public",
    downloads_count: int = 0,
) -> Repo:
    """Insert a Repo row directly (skips the HTTP API)."""
    from sqlalchemy.orm import selectinload

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
            downloads_count=downloads_count,
        )
        session.add(repo)
        await session.commit()
        repo_id = repo.id
    async with factory() as session:
        return (
            await session.execute(
                select(Repo).where(Repo.id == repo_id).options(selectinload(Repo.owner))
            )
        ).scalar_one()


async def _seed_post(
    factory: async_sessionmaker,
    *,
    author: User,
    body: str = "hello",
    kind: str = "short",
    title: str | None = None,
    repo: Repo | None = None,
) -> int:
    """Insert a Post row directly; return the post id."""
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
# Home page surface
# ---------------------------------------------------------------------------


class TestHomeSurface:
    """`GET /` is the mixed-feed home page, not the legacy catalog."""

    async def test_home_does_not_render_legacy_heading(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        assert "Public repositories" not in response.text

    async def test_home_renders_empty_state_when_no_data(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        assert "The community has not published anything yet" in body

    async def test_home_includes_all_kind_catalog_links(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        body = response.text
        for needle in (
            'href="/models"',
            'href="/datasets"',
            'href="/spaces"',
            'href="/posts"',
        ):
            assert needle in body, needle


# ---------------------------------------------------------------------------
# Mixed feed tiles
# ---------------------------------------------------------------------------


class TestHomeFeedTiles:
    """The home renders all four tile kinds (post + model + dataset + space)."""

    async def test_home_includes_post_tile(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_post(factory, author=alice, body="hello home page")

        response = client.get("/")
        body = response.text
        # The kind pill inside the tile marks it as a Post.
        assert ">Post<" in body
        # The post body surfaces in the tile (rendered as escaped text).
        assert "hello home page" in body

    async def test_home_includes_top_models_tile(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_repo(
                factory,
                owner=alice,
                name="top-model",
                kind="model",
                downloads_count=42,
            )

        response = client.get("/")
        body = response.text
        assert ">Model<" in body
        assert "alice/top-model" in body
        # The download counter is rendered as a stat pill.
        assert "42" in body

    async def test_home_includes_top_datasets_tile(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_repo(
                factory,
                owner=alice,
                name="top-dataset",
                kind="dataset",
                downloads_count=99,
            )

        response = client.get("/")
        body = response.text
        assert ">Dataset<" in body
        assert "alice/top-dataset" in body

    async def test_home_includes_spaces_tile(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_repo(factory, owner=alice, name="top-space", kind="space")

        response = client.get("/")
        body = response.text
        assert ">Space<" in body
        assert "alice/top-space" in body
        # The footnote about "recently updated" vs "longest running"
        # appears so the operator does not mistake the tile for uptime.
        assert "ordered by most-recent update" in body

    async def test_home_mixes_all_four_kinds_in_one_response(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_post(factory, author=alice, body="mixed post")
            await _seed_repo(factory, owner=alice, name="m1", kind="model", downloads_count=1)
            await _seed_repo(factory, owner=alice, name="d1", kind="dataset", downloads_count=2)
            await _seed_repo(factory, owner=alice, name="s1", kind="space")

        response = client.get("/")
        body = response.text
        # All four tile kinds render in the same response.
        assert ">Post<" in body
        assert ">Model<" in body
        assert ">Dataset<" in body
        assert ">Space<" in body


# ---------------------------------------------------------------------------
# Visibility rules
# ---------------------------------------------------------------------------


class TestHomeVisibilityRules:
    """Private repos do NOT appear in the home tile lists."""

    async def test_private_repo_excluded_from_top_models(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_repo(
                factory,
                owner=alice,
                name="hidden-model",
                kind="model",
                visibility="private",
                downloads_count=1000,
            )

        response = client.get("/")
        assert "alice/hidden-model" not in response.text

    async def test_private_dataset_excluded_from_top_datasets(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_repo(
                factory,
                owner=alice,
                name="hidden-dataset",
                kind="dataset",
                visibility="private",
            )

        response = client.get("/")
        assert "alice/hidden-dataset" not in response.text

    async def test_private_space_excluded_from_running_spaces(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_repo(
                factory,
                owner=alice,
                name="hidden-space",
                kind="space",
                visibility="private",
            )

        response = client.get("/")
        assert "alice/hidden-space" not in response.text


# ---------------------------------------------------------------------------
# Trending posts
# ---------------------------------------------------------------------------


class TestHomeTrendingPosts:
    """The trending tile prefers posts with the most reactions in the window."""

    async def test_trending_orders_by_reaction_count(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        bob_id = await seed_approved_user(username="bob")
        carol_id = await seed_approved_user(username="carol")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            bob = (await session.execute(select(User).where(User.id == bob_id))).scalar_one()
            carol = (await session.execute(select(User).where(User.id == carol_id))).scalar_one()
            popular_id = await _seed_post(factory, author=alice, body="popular post")
            quiet_id = await _seed_post(factory, author=alice, body="quiet post")
            # `popular` gets reactions from 3 distinct users; `quiet`
            # gets only 1 reaction. The trending query should rank
            # `popular` above `quiet`.
            for user in (alice, bob, carol):
                session.add(PostReaction(post_id=popular_id, user_id=user.id, emoji="\U0001f44d"))
            session.add(PostReaction(post_id=quiet_id, user_id=alice.id, emoji="\U0001f44d"))

        response = client.get("/")
        body = response.text
        popular_pos = body.find("popular post")
        quiet_pos = body.find("quiet post")
        assert popular_pos != -1 and quiet_pos != -1
        assert popular_pos < quiet_pos, "trending tile must rank popular post first"

    async def test_trending_falls_back_to_recent_when_no_reactions(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker,
    ) -> None:
        client, _, _ = app
        alice_id = await seed_approved_user(username="alice")
        async with factory() as session:
            alice = (await session.execute(select(User).where(User.id == alice_id))).scalar_one()
            await _seed_post(factory, author=alice, body="no reactions yet")

        response = client.get("/")
        assert response.status_code == 200
        assert "no reactions yet" in response.text


__all__ = [
    "TestHomeFeedTiles",
    "TestHomeSurface",
    "TestHomeTrendingPosts",
    "TestHomeVisibilityRules",
]
