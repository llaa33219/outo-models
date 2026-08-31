"""Async session factory and per-request scope context manager.

Two layers so callers can pick the right tool: `get_session_factory` for
dependency-injection in FastAPI (one factory per engine, lifetime-tied),
and `session_scope` for one-off scripts that want commit-on-success /
rollback-on-error semantics without a dependency-injection framework.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.db.engine import get_engine


def get_session_factory(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    """Return a fresh `async_sessionmaker` bound to `engine`.

    `engine=None` reuses the process-wide engine from `get_engine()`. The
    factory itself is cheap to construct; caches by caller convention.
    The default `expire_on_commit=False` is critical — it keeps ORM
    attributes usable after `commit()` so the request handler can read
    them without an extra refresh round-trip.
    """
    if engine is None:
        engine = get_engine()
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield an `AsyncSession`; commit on success, rollback on exception, always close.

    Usage:
            async with session_scope() as session:
                session.add(...)
                # commit happens automatically on clean exit

        Any exception inside the `with` block triggers a rollback and is
        re-raised after cleanup so callers do not need a separate `try /
        except` to surface the original error.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


__all__ = ["get_session_factory", "session_scope"]