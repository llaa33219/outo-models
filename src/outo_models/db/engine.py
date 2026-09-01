"""Async SQLAlchemy engine factory for outo-models.

The engine is cached per `db_url` so callers get the same connection pool for
the same database. Sqlite-specific pragmas (WAL journal mode, busy timeout)
are applied via a `connect` event listener and are the only place SQL is
specialized; the model layer stays dialect-agnostic.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from outo_models.config import Settings, get_settings


def _is_sqlite_url(db_url: str) -> bool:
    """Return True iff `db_url` targets SQLite (any `sqlite` dialect variant)."""
    return db_url.startswith("sqlite")


def _sqlite_connect_listener(
    dbapi_connection: sqlite3.Connection, _connection_record: object
) -> None:
    """Apply WAL + busy-timeout pragmas on every new sqlite DBAPI connection.

    SQLAlchemy's `connect` event fires with `(dbapi_connection, record)`; we
    run the PRAGMAs on the raw `sqlite3.Connection` so they take effect
    before any application SQL is issued on the borrowed handle.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


# Track every engine we have handed out so `dispose_engines` can dispose all
# of them during test teardown. The dict is keyed by `db_url` to mirror the
# `lru_cache` on `get_engine`.
_ENGINES: dict[str, AsyncEngine] = {}


@lru_cache(maxsize=8)
def _build_engine(db_url: str) -> AsyncEngine:
    """Construct a single `AsyncEngine` for `db_url` and register sqlite pragmas.

    The cache and the `_ENGINES` registry must stay in sync — every cache hit
    yields the same engine instance and `_ENGINES` is updated by reference.
    """
    connect_args: dict[str, object] = {}
    if _is_sqlite_url(db_url):
        # Sqlite's default check_same_thread=True breaks the async driver
        # because each task hop crosses threads. The driver serializes
        # access internally; the thread check is a paranoid relic.
        connect_args["check_same_thread"] = False
    engine = create_async_engine(db_url, connect_args=connect_args)
    if _is_sqlite_url(db_url):
        event.listen(engine.sync_engine, "connect", _sqlite_connect_listener)
    return engine


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide `AsyncEngine` for `settings.resolved_db_url`.

    The engine is created on first access and reused thereafter — the
    `lru_cache` ensures the same `db_url` always yields the same engine and
    therefore the same connection pool. `settings=None` uses the cached
    process-wide `Settings` from `get_settings()`.
    """
    if settings is None:
        settings = get_settings()
    db_url = settings.resolved_db_url
    engine = _build_engine(db_url)
    _ENGINES.setdefault(db_url, engine)
    return engine


async def dispose_engines() -> None:
    """Dispose every cached engine. Intended as a test-isolation helper.

    After calling this, the next `get_engine(...)` invocation will build a
    fresh engine (the `lru_cache` is also cleared so the constructor runs
    again). Production code does not call this; tests do, between cases.
    """
    for engine in list(_ENGINES.values()):
        await engine.dispose()
    _ENGINES.clear()
    _build_engine.cache_clear()


async def run_migrations(engine: AsyncEngine) -> None:
    """Programmatic `alembic upgrade head` against `engine`.

    Wraps the sync alembic CLI in a worker thread. env.py already detects
    a running event loop and routes migrations through its own worker
    thread; this wrapper makes `run_migrations` itself awaitable so the
    FastAPI startup task and pytest-asyncio tests can call it directly.

    The URL passed to alembic is the async driver's URL
    (`sqlite+aiosqlite://...`) so env.py builds an `AsyncEngine`. On
    non-sqlite dialects the URL is forwarded unchanged.
    """
    import asyncio
    import concurrent.futures

    from alembic import command
    from alembic.config import Config

    from outo_models import version

    cfg = Config()
    cfg.set_main_option("script_location", "src/outo_models/db/migrations")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    cfg.set_main_option("outo_version", version.__version__)

    def _run() -> None:
        command.upgrade(cfg, "head")

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        await loop.run_in_executor(executor, _run)


__all__ = ["dispose_engines", "get_engine", "run_migrations"]
