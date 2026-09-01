"""Integration tests for `outo_models.spaces.registry`.

Every public registry function is exercised end-to-end against a real sqlite
engine + real `tmp_data_dir`, so the test doubles as documentation of the
on-disk layout and the DB row shape WP-13 will see:

    - `Repo(kind="space")` rows are produced by `create_space` via
      `repos.create.create_repo`; nothing extra lives in the schema.
    - The chosen `sdk` is recorded in `<spaces_dir>/<owner>/<name>.json`,
      not in the DB row. The DB row stays schema-stable; the filesystem
      is the source of truth for SDK choice.
    - Visibility filtering, update, and delete all flow through the
      existing `Repo` machinery — so the audit / quota / lock behavior
      already covered by `test_repo_lifecycle` carries over here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    Repo,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.exceptions import NotFoundError, ValidationFailedError
from outo_models.repos.models import Visibility
from outo_models.spaces import registry as registry_mod
from outo_models.spaces.registry import (
    SUPPORTED_SDKS,
    SpaceMeta,
    create_space,
    delete_space,
    get_space,
    list_spaces,
    read_space_meta,
    update_space,
    write_space_meta,
)
from outo_models.utils.paths import spaces_dir

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh per-test sqlite-backed engine + schema; auto-disposed."""
    await dispose_engines()
    settings = get_settings()
    engine: AsyncEngine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = get_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
        await dispose_engines()


async def _make_user(factory: async_sessionmaker[AsyncSession], username: str) -> User:
    """Create + return a `User` row from a fresh session."""
    async with factory() as session:
        user = User(username=username, email=f"{username}@example.com", password_hash="h")
        session.add(user)
        await session.commit()
        return (await session.execute(select(User).where(User.username == username))).scalar_one()


async def _reload_user(factory: async_sessionmaker[AsyncSession], username: str) -> User:
    """Look up `User` from a fresh session — the caller's session may be closed."""
    async with factory() as session:
        return (await session.execute(select(User).where(User.username == username))).scalar_one()


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestSupportedSdks:
    """The supported SDK set is a frozen tuple in the order documented by WP-13."""

    def test_is_tuple_of_four_known_sdks(self) -> None:
        assert SUPPORTED_SDKS == ("static", "gradio", "streamlit", "docker")

    def test_default_sdk_is_static(self) -> None:
        # The default value of `create_space(... sdk="static")` MUST appear in
        # `SUPPORTED_SDKS`; otherwise the first call would raise its own error.
        assert "static" in SUPPORTED_SDKS


# ---------------------------------------------------------------------------
# write_space_meta / read_space_meta helpers
# ---------------------------------------------------------------------------


class TestSpaceMetaHelpers:
    """The on-disk JSON file under `spaces_dir()/owner/name.json` is the source of truth."""

    async def test_write_then_read_round_trips_sdk(self, tmp_data_dir: Path) -> None:
        meta = SpaceMeta(sdk="gradio")
        write_space_meta("alice", "demo", meta)

        assert read_space_meta("alice", "demo").sdk == "gradio"

    async def test_read_missing_meta_returns_static_default(self, tmp_data_dir: Path) -> None:
        # No file written; the contract is "missing → default sdk static".
        meta = read_space_meta("alice", "absent")
        assert meta.sdk == "static"

    async def test_meta_file_lives_under_spaces_dir(self, tmp_data_dir: Path) -> None:
        write_space_meta("alice", "demo", SpaceMeta(sdk="docker"))

        path = spaces_dir() / "alice" / "demo.json"
        assert path.is_file()
        # JSON shape: keys we care about are present, no extras leak in.
        payload = json.loads(path.read_text())
        assert payload["sdk"] == "docker"
        assert "updated_at" in payload

    async def test_write_creates_owner_segment(self, tmp_data_dir: Path) -> None:
        # The owner dir must NOT exist before the first write — proving we
        # are not silently relying on a pre-created directory.
        assert not (spaces_dir() / "alice").exists()

        write_space_meta("alice", "demo", SpaceMeta(sdk="streamlit"))

        assert (spaces_dir() / "alice").is_dir()


# ---------------------------------------------------------------------------
# create_space
# ---------------------------------------------------------------------------


class TestCreateSpace:
    """`create_space` produces a `Repo(kind="space")` plus an on-disk meta file."""

    async def test_create_writes_repo_row_with_kind_space(
        self, factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        owner = await _make_user(factory, "alice")

        async with factory() as session:
            repo = await create_space(session, owner=owner, name="demo")
            await session.commit()

        # `Repo.kind` is stored as a string column; the contract is "space".
        assert repo.kind == "space"
        async with factory() as session:
            row = (await session.execute(select(Repo).where(Repo.id == repo.id))).scalar_one()
            assert row.kind == "space"
            assert row.visibility == "private"
            assert row.default_branch == "main"

    async def test_create_writes_meta_file_with_chosen_sdk(
        self, factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        owner = await _make_user(factory, "alice")

        async with factory() as session:
            await create_space(session, owner=owner, name="demo", sdk="gradio")
            await session.commit()

        meta = read_space_meta("alice", "demo")
        assert meta.sdk == "gradio"

    async def test_create_default_sdk_is_static(
        self, factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        owner = await _make_user(factory, "alice")

        async with factory() as session:
            await create_space(session, owner=owner, name="demo")
            await session.commit()

        assert read_space_meta("alice", "demo").sdk == "static"

    async def test_create_bare_repo_materializes_on_disk(
        self, factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        from outo_models.repos.storage import repo_fs_path

        owner = await _make_user(factory, "alice")

        async with factory() as session:
            await create_space(session, owner=owner, name="demo")
            await session.commit()

        # `create_repo` (the underlying call) writes the bare git repo at
        # `repos_dir/<owner>/<name>.git`. Confirm the layout is real.
        assert repo_fs_path("alice", "demo").is_dir()

    async def test_create_persists_visibility_and_description(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _make_user(factory, "alice")

        async with factory() as session:
            repo = await create_space(
                session,
                owner=owner,
                name="demo",
                visibility=Visibility.PUBLIC,
                description="hello world",
            )
            repo_id = repo.id
            await session.commit()

        async with factory() as session:
            row = (await session.execute(select(Repo).where(Repo.id == repo_id))).scalar_one()
            assert row.visibility == "public"
            assert row.description == "hello world"

    async def test_create_with_invalid_sdk_raises_validation(
        self, factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        owner = await _make_user(factory, "alice")

        async with factory() as session:
            with pytest.raises(ValidationFailedError):
                await create_space(session, owner=owner, name="demo", sdk="vim")
            await session.rollback()

        # No bare repo on disk and no meta file — the validation runs
        # BEFORE the delegation to `create_repo`.
        from outo_models.repos.storage import repo_exists

        assert not repo_exists("alice", "demo")
        assert not (spaces_dir() / "alice" / "demo.json").exists()


# ---------------------------------------------------------------------------
# get_space
# ---------------------------------------------------------------------------


class TestGetSpace:
    """`get_space` returns the row only when the kind matches."""

    async def test_returns_row_with_owner_loaded(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            await create_space(session, owner=owner, name="demo")
            await session.commit()

        async with factory() as session:
            space = await get_space(session, owner_name="alice", name="demo")

        assert space.name == "demo"
        assert space.kind == "space"
        # Owner must be eager-loaded — WP-13 routers render `owner.username`
        # without issuing a second query.
        assert space.owner.username == "alice"

    async def test_missing_space_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _make_user(factory, "alice")

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await get_space(session, owner_name="alice", name="nope")

    async def test_wrong_kind_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A model repo must not be returned by `get_space` even if the name
        # collides — kind discrimination is the safety property.
        from outo_models.repos.create import create_repo
        from outo_models.repos.models import RepoKind

        owner = await _make_user(factory, "alice")
        async with factory() as session:
            await create_repo(session, owner=owner, name="collide", kind=RepoKind.MODEL)
            await session.commit()

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await get_space(session, owner_name="alice", name="collide")

    async def test_unknown_owner_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            with pytest.raises(NotFoundError):
                await get_space(session, owner_name="ghost", name="demo")


# ---------------------------------------------------------------------------
# list_spaces
# ---------------------------------------------------------------------------


class TestListSpaces:
    """`list_spaces` paginates and respects the visibility contract."""

    async def test_empty_list_when_no_spaces(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            assert await list_spaces(session) == []

    async def test_returns_only_kind_space(self, factory: async_sessionmaker[AsyncSession]) -> None:
        from outo_models.repos.create import create_repo
        from outo_models.repos.models import RepoKind

        owner = await _make_user(factory, "alice")
        async with factory() as session:
            await create_repo(session, owner=owner, name="model-a", kind=RepoKind.MODEL)
            await create_space(session, owner=owner, name="space-a")
            await create_space(session, owner=owner, name="space-b")
            await session.commit()

        async with factory() as session:
            spaces = await list_spaces(session, owner_name="alice", include_private=True)

        names = sorted(s.name for s in spaces)
        assert names == ["space-a", "space-b"]

    async def test_default_hides_private_spaces(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            await create_space(session, owner=owner, name="pub", visibility=Visibility.PUBLIC)
            await create_space(session, owner=owner, name="priv", visibility=Visibility.PRIVATE)
            await session.commit()

        async with factory() as session:
            names = sorted(s.name for s in await list_spaces(session))

        assert names == ["pub"]

    async def test_include_private_returns_all(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            await create_space(session, owner=owner, name="pub", visibility=Visibility.PUBLIC)
            await create_space(session, owner=owner, name="priv", visibility=Visibility.PRIVATE)
            await session.commit()

        async with factory() as session:
            names = sorted(s.name for s in await list_spaces(session, include_private=True))

        assert names == ["priv", "pub"]

    async def test_owner_name_filter(self, factory: async_sessionmaker[AsyncSession]) -> None:
        alice = await _make_user(factory, "alice")
        bob = await _make_user(factory, "bob")

        async with factory() as session:
            await create_space(session, owner=alice, name="a-one", visibility=Visibility.PUBLIC)
            await create_space(session, owner=bob, name="b-one", visibility=Visibility.PUBLIC)
            await session.commit()

        async with factory() as session:
            spaces = await list_spaces(session, owner_name="alice")

        assert [s.name for s in spaces] == ["a-one"]

    async def test_limit_and_offset_paginate(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            for i in range(5):
                await create_space(
                    session,
                    owner=owner,
                    name=f"s-{i}",
                    visibility=Visibility.PUBLIC,
                )
            await session.commit()

        async with factory() as session:
            page_1 = await list_spaces(session, limit=2, offset=0, include_private=True)
            page_2 = await list_spaces(session, limit=2, offset=2, include_private=True)

        assert [s.name for s in page_1] == ["s-0", "s-1"]
        assert [s.name for s in page_2] == ["s-2", "s-3"]


# ---------------------------------------------------------------------------
# update_space
# ---------------------------------------------------------------------------


class TestUpdateSpace:
    """`update_space` mutates visibility and description on the row."""

    async def test_update_visibility(self, factory: async_sessionmaker[AsyncSession]) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            repo = await create_space(session, owner=owner, name="demo")
            await session.commit()

        async with factory() as session:
            updated = await update_space(session, space=repo, visibility=Visibility.PUBLIC)
            await session.commit()

        async with factory() as session:
            row = (await session.execute(select(Repo).where(Repo.id == updated.id))).scalar_one()
            assert row.visibility == "public"

    async def test_update_description(self, factory: async_sessionmaker[AsyncSession]) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            repo = await create_space(session, owner=owner, name="demo")
            await session.commit()

        async with factory() as session:
            updated = await update_space(session, space=repo, description="new desc")
            await session.commit()

        async with factory() as session:
            row = (await session.execute(select(Repo).where(Repo.id == updated.id))).scalar_one()
            assert row.description == "new desc"

    async def test_update_persists(self, factory: async_sessionmaker[AsyncSession]) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            repo = await create_space(session, owner=owner, name="demo")
            await session.commit()
            repo_id = repo.id

        async with factory() as session:
            await update_space(
                session,
                space=repo,
                visibility=Visibility.PUBLIC,
                description="updated",
            )
            await session.commit()

        async with factory() as session:
            row = (await session.execute(select(Repo).where(Repo.id == repo_id))).scalar_one()
            assert row.visibility == "public"
            assert row.description == "updated"


# ---------------------------------------------------------------------------
# delete_space
# ---------------------------------------------------------------------------


class TestDeleteSpace:
    """`delete_space` removes the row, the bare repo, and the meta file."""

    async def test_delete_removes_row_and_meta_file(
        self, factory: async_sessionmaker[AsyncSession], tmp_data_dir: Path
    ) -> None:
        owner = await _make_user(factory, "alice")
        async with factory() as session:
            repo = await create_space(session, owner=owner, name="demo")
            await session.commit()
            repo_id = repo.id

        meta_path = spaces_dir() / "alice" / "demo.json"
        assert meta_path.is_file()

        async with factory() as session:
            await delete_space(session, owner=owner, name="demo")
            await session.commit()

        async with factory() as session:
            row = (
                await session.execute(select(Repo).where(Repo.id == repo_id))
            ).scalar_one_or_none()
            assert row is None

        assert not meta_path.exists()

    async def test_delete_missing_raises_not_found(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _make_user(factory, "alice")

        async with factory() as session:
            with pytest.raises(NotFoundError):
                await delete_space(session, owner=owner, name="nope")


# ---------------------------------------------------------------------------
# Imports smoke check (lock the public re-exports WP-13 will rely on)
# ---------------------------------------------------------------------------


class TestRegistryPublicSurface:
    """The names documented in the API contract are importable from the module."""

    def test_all_documented_names_are_importable(self) -> None:
        for name in (
            "SUPPORTED_SDKS",
            "SpaceMeta",
            "create_space",
            "delete_space",
            "get_space",
            "list_spaces",
            "read_space_meta",
            "update_space",
            "write_space_meta",
        ):
            assert hasattr(registry_mod, name), f"registry missing {name!r}"
