"""Round-trip tests for the `WebSetting` ORM model.

Covers create / read / update / delete and the unique constraint on `key`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.config import get_settings
from outo_models.db import Base, WebSetting, dispose_engines, get_engine, get_session_factory


@pytest.fixture
async def session_factory(tmp_data_dir) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


class TestWebSettingCreateRead:
    """`WebSetting` rows round-trip with the operator-supplied payload."""

    async def test_create_and_read_back(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(WebSetting(key="homepage.banner", value="Welcome to outo-models"))
            await session.commit()

        async with session_factory() as session:
            setting = (
                await session.execute(select(WebSetting).where(WebSetting.key == "homepage.banner"))
            ).scalar_one()
            assert setting.value == "Welcome to outo-models"

    async def test_create_with_empty_value(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # `value` is a free-form string; empty is allowed.
        async with session_factory() as session:
            session.add(WebSetting(key="homepage.subtitle", value=""))
            await session.commit()

        async with session_factory() as session:
            setting = (
                await session.execute(
                    select(WebSetting).where(WebSetting.key == "homepage.subtitle")
                )
            ).scalar_one()
            assert setting.value == ""


class TestWebSettingUpdate:
    """`value` is the operator-editable field; updating it is a normal flow."""

    async def test_update_value(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            session.add(WebSetting(key="homepage.banner", value="first"))
            await session.commit()
            setting_id = (
                await session.execute(
                    select(WebSetting.id).where(WebSetting.key == "homepage.banner")
                )
            ).scalar_one()

        async with session_factory() as session:
            setting = await session.get(WebSetting, setting_id)
            assert setting is not None
            setting.value = "second"
            await session.commit()

        async with session_factory() as session:
            setting = await session.get(WebSetting, setting_id)
            assert setting is not None
            assert setting.value == "second"


class TestWebSettingDelete:
    """Delete removes the row."""

    async def test_delete(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            session.add(WebSetting(key="homepage.banner", value="x"))
            await session.commit()
            setting_id = (
                await session.execute(
                    select(WebSetting.id).where(WebSetting.key == "homepage.banner")
                )
            ).scalar_one()

        async with session_factory() as session:
            setting = await session.get(WebSetting, setting_id)
            assert setting is not None
            await session.delete(setting)
            await session.commit()

        async with session_factory() as session:
            assert (
                await session.execute(select(WebSetting).where(WebSetting.key == "homepage.banner"))
            ).scalar_one_or_none() is None


class TestWebSettingUniqueKey:
    """`key` is unique; reusing a key collides on insert."""

    async def test_duplicate_key_raises_integrity_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(WebSetting(key="homepage.banner", value="a"))
            await session.commit()

        async with session_factory() as session:
            session.add(WebSetting(key="homepage.banner", value="b"))
            with pytest.raises(IntegrityError):
                await session.commit()
