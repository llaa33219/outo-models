"""Shared fixtures for the server integration tests.

The `app` fixture builds a fresh `FastAPI` via `create_app(settings)`
against a per-test tmpdir; tests get a fully-wired HTTP stack (routers,
middleware, exception handlers, git smart-HTTP mount, lifespan
migrations + scheduler teardown) without standing up a real uvicorn.

The mount order is the one `create_app` documents: routers first, then
the git smart-HTTP service at root, so HF-style URLs (`/owner/name.git`)
resolve through the git ASGI app and JSON / HTML routes resolve through
the registered routers.

Run budget: < 5 s per module. The lifespan pays one `alembic upgrade`
and one APScheduler start per test, which dominates the wall clock.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.auth.passwords import hash_password
from outo_models.config import get_settings
from outo_models.db import (
    Base,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.server import create_app


@pytest.fixture
def app(tmp_data_dir: Path) -> Iterator:
    """Yield a fresh `FastAPI` app bound to `tmp_data_dir`.

    Sets `OUTO_SECRET_KEY` to a deterministic non-empty string so the
    session / CSRF serializers are stable across tests. Closes the
    underlying engine on teardown so successive tests get a clean pool.

    Also clears slowapi's in-memory rate-limit buckets so per-test login
    counters cannot bleed between cases.
    """
    os.environ.setdefault("OUTO_SECRET_KEY", "test-secret-key-for-server-tests-1234567890")
    get_settings.cache_clear()
    settings = get_settings()
    # Reset slowapi's in-memory limiter state between tests so a barrage
    # of logins in one case does not exhaust the per-IP quota for the next.
    from outo_models.auth.rate_limit import limiter

    limiter.reset()
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            yield client, app, settings
    finally:
        limiter.reset()
        get_settings.cache_clear()


@pytest.fixture
async def factory(tmp_data_dir: Path) -> async_sessionmaker[AsyncSession]:
    """Per-test sqlite engine + session factory for direct DB seeding.

    Async fixture: tests that need a pre-seeded user / admin use this to
    bypass the HTTP signup endpoint and inject rows directly, then go
    through the HTTP API to exercise the actual router code path.
    """
    await dispose_engines()
    settings = get_settings()
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = get_session_factory(engine)
    try:
        yield sf
    finally:
        await dispose_engines()


async def _seed_approved_user(
    factory_local: async_sessionmaker[AsyncSession],
    *,
    username: str,
    role: str = "user",
) -> int:
    """Insert an approved user; return `user.id`."""
    async with factory_local() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("correct horse battery staple"),
            role=role,
            status="approved",
        )
        session.add(user)
        await session.commit()
        user_id = user.id
        # Make sure quota + usage rows exist so /me has stable shape.
        session.add(UserQuota(user_id=user_id, max_bytes=10 * 1024**3))
        session.add(UserUsage(user_id=user_id, used_bytes=0))
        await session.commit()
        return user_id


@pytest.fixture
def seed_approved_user(factory: async_sessionmaker[AsyncSession]):
    """Fixture that returns an async seeder: `await seed_approved_user(...)`."""

    async def _seed(*, username: str, role: str = "user") -> int:
        return await _seed_approved_user(factory, username=username, role=role)

    return _seed


__all__ = ["app", "factory", "seed_approved_user"]
