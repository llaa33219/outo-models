"""Alembic environment for outo-models.

The default Alembic env is synchronous against a single SQLAlchemy `Engine`,
but our application uses `AsyncEngine` end-to-end. We bridge the two by
running the migration runner inside `connection.run_sync(...)`, which lets
Alembic's imperative `op.*` calls execute against the async connection's
underlying sync DBAPI connection without spawning a second engine.

The DB URL is read from `OUTO_DB_URL` and falls back to the process-wide
`Settings.resolved_db_url` so production deployments need no extra flags.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from outo_models.config import get_settings
from outo_models.db.models import Base

# Alembic Config object — values from `alembic.ini` flow through this.
config = context.config

# Configure Python logging from the ini file when a `[loggers]` section is
# present. Tests skip this because they call `upgrade()` / `downgrade()`
# directly without spinning up Alembic's CLI.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Bind the model's metadata so `--autogenerate` can diff against it.
target_metadata = Base.metadata


def _resolve_db_url() -> str:
    """Return the migration target URL: OUTO_DB_URL, then Settings, then ini.

    The OUTO_DB_URL override exists so the production container's entrypoint
    can point alembic at the real database without re-rendering `alembic.ini`.
    Sync `sqlite:///...` URLs are transparently rewritten to
    `sqlite+aiosqlite:///...` because the migration runner builds an
    `AsyncEngine`; the async driver is required even for sync `command.*`
    invocations because the SQLAlchemy 2.0 async engine rejects sync
    drivers at construction time.
    """
    env_url = os.environ.get("OUTO_DB_URL")
    url = env_url if env_url else get_settings().resolved_db_url
    if url.startswith("sqlite:///") and "+aiosqlite" not in url:
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout for offline review. Not used in production."""
    url = _resolve_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migration set against a live sync `connection`.

    `render_as_batch=True` so SQLite gets the ALTER TABLE emulation it
    requires (sqlite cannot do `ALTER TABLE ... ALTER COLUMN ...`); on
    postgres the same call has no effect.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against an `AsyncEngine` driven by the env config."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_db_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Sync entrypoint alembic's CLI invokes; bridges to async.

    When invoked from a sync context (the alembic CLI), `asyncio.run`
    works as-is. When invoked from inside a running event loop — e.g.
    a pytest-asyncio test or a FastAPI startup task — `asyncio.run`
    would raise `RuntimeError: asyncio.run() cannot be called from a
    running event loop`. The workaround is to run the migrations on a
    fresh thread that owns its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations())
        return

    import threading

    holder: list[Exception | None] = [None]

    def _worker() -> None:
        try:
            asyncio.run(run_async_migrations())
        except Exception as exc:
            holder[0] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if holder[0] is not None:
        raise holder[0]


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()