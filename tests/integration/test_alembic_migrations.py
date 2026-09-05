"""Integration tests for the Alembic migration pipeline.

The contract is twofold:
    1. `alembic upgrade head` creates every table.
    2. `alembic upgrade head` -> `downgrade base` -> `upgrade head` is a no-op
       against the schema -- i.e. the down + up round-trip is reproducible.

We compare schemas via `sqlalchemy.inspect(...)` so the test does not depend
on a specific order or count of indexes (only the visible, named objects).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from outo_models.config import get_settings
from outo_models.db import (
    Base,
    dispose_engines,
    get_engine,
    run_migrations,
)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Build a per-test sqlite-backed engine + create schema; dispose after."""
    await dispose_engines()
    settings = get_settings()
    eng = get_engine(settings)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        await dispose_engines()


def _schema_snapshot(sync_conn: object) -> set[tuple[str, ...]]:
    """Return a normalized set of `(table, column, type)` tuples from the DB.

    Two schemas are "the same" iff this function returns the same set on
    both -- independent of index ordering or constraint wording.
    """
    insp = inspect(sync_conn)
    rows: set[tuple[str, ...]] = set()
    for table_name in insp.get_table_names():
        for column in insp.get_columns(table_name):
            rows.add((table_name, column["name"], str(column["type"])))
    return rows


def _table_names(sync_conn: object) -> set[str]:
    """Return the set of user-visible tables (no `alembic_version`)."""
    insp = inspect(sync_conn)
    return set(insp.get_table_names())


class TestMigrationsApply:
    """`alembic upgrade head` produces a schema with every ORM table."""

    async def test_upgrade_creates_every_model_table(self, tmp_data_dir: Path) -> None:
        await dispose_engines()
        settings = get_settings()
        eng = get_engine(settings)
        try:
            await run_migrations(eng)
            async with eng.connect() as conn:
                tables = await conn.run_sync(_table_names)
            expected = set(Base.metadata.tables)
            assert expected.issubset(tables), f"missing tables: {expected - tables}"
        finally:
            await eng.dispose()
            await dispose_engines()

    async def test_upgrade_adds_0002_social_columns(self, tmp_data_dir: Path) -> None:
        await dispose_engines()
        settings = get_settings()
        eng = get_engine(settings)
        try:
            await run_migrations(eng)
            async with eng.connect() as conn:
                tables = await conn.run_sync(_table_names)
            assert {"repo_likes", "user_follows", "repo_comments"} <= tables

            def _repo_col_names(sync_conn: object) -> set[str]:
                return {c["name"] for c in inspect(sync_conn).get_columns("repos")}

            async with eng.connect() as conn:
                repo_cols = await conn.run_sync(_repo_col_names)
            assert "downloads_count" in repo_cols
        finally:
            await eng.dispose()
            await dispose_engines()


class TestMigrationRoundTrip:
    """`upgrade head -> downgrade base -> upgrade head` is a no-op on the schema."""

    async def test_round_trip_is_a_noop(self, tmp_data_dir: Path) -> None:
        await dispose_engines()
        settings = get_settings()
        eng = get_engine(settings)
        try:
            cfg = Config()
            cfg.set_main_option("script_location", "src/outo_models/db/migrations")
            cfg.set_main_option("sqlalchemy.url", eng.url.render_as_string(hide_password=False))

            # Start clean so we measure exactly what migrations produce.
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)

            command.upgrade(cfg, "head")
            async with eng.connect() as conn:
                baseline = await conn.run_sync(_schema_snapshot)

            # Downgrade to base, then upgrade back to head.
            command.downgrade(cfg, "base")
            command.upgrade(cfg, "head")

            async with eng.connect() as conn:
                after_round_trip = await conn.run_sync(_schema_snapshot)

            assert baseline == after_round_trip
        finally:
            await eng.dispose()
            await dispose_engines()
