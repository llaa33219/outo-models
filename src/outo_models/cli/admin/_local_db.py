"""Local DB helpers backing the `outo-models admin ...` commands.

Each command body in `admin._commands` calls one of these helpers on the
local-DB path; the remote path goes through `AdminApiClient` instead.
Typer command bodies are sync; SQLAlchemy session APIs are async, so we
mirror every helper as `_*_async` (the SQL) plus a sync wrapper that
runs it via `asyncio.run`.

# allow: SIZE_OK — every admin endpoint needs its own async/sync pair
# (10+ pairs of nearly-identical CRUD bodies); splitting into per-resource
# sub-modules would force every command site to thread an extra import.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from outo_models.auth import (
    approve_user as _approve_user,
)
from outo_models.auth import (
    ban_user as _ban_user,
)
from outo_models.auth import (
    deny_user as _deny_user,
)
from outo_models.auth import (
    unban_user as _unban_user,
)
from outo_models.auth.passwords import hash_password
from outo_models.db import (
    AuditLog,
    User,
    WebSetting,
    get_engine,
    get_session_factory,
)
from outo_models.db.engine import run_migrations
from outo_models.db.session import session_scope
from outo_models.exceptions import NotFoundError
from outo_models.utils.slug import validate_slug


def run_async(coro: Any) -> Any:
    """Run a coroutine from a sync caller via `asyncio.run`."""
    return asyncio.run(coro)


def session_factory() -> async_sessionmaker[Any]:
    """Return a fresh session factory bound to the engine of `get_settings()`."""
    from outo_models.config import get_settings

    return get_session_factory(get_engine(get_settings()))


async def bootstrap_if_needed() -> None:
    """Ensure the schema exists via alembic.

    Local-DB CLI commands must not require the operator to run
    `outo-models server migrate` first. We delegate to `run_migrations`
    rather than `Base.metadata.create_all` because alembic tracks the
    schema version in `alembic_version` — using both is what raised
    `table users already exists` on the second call in tests.
    """
    from outo_models.config import get_settings

    engine = get_engine(get_settings())
    await run_migrations(engine)


async def fetch_first_admin() -> Any:
    factory = session_factory()
    async with factory() as session:
        result = await session.execute(
            select(User).where(User.role == "admin").order_by(User.id)
        )
        return result.scalars().first()


def require_admin_for_local() -> Any:
    """Return the first admin row, used as the AuditLog actor for local admin commands.

    The CLI is operator-facing; we do not gate on authentication — the
    operator is assumed to have shell access to the host already. The
    "actor" identity is the first admin in the table (created by
    `setup`). Returns `None` when no admin exists.
    """
    return run_async(fetch_first_admin())


# list / pending ----------------------------------------------------------------


async def _list_users_async(status_filter: str | None) -> list[dict[str, Any]]:
    await bootstrap_if_needed()
    factory = session_factory()
    async with factory() as session:
        stmt = select(User).order_by(User.id)
        if status_filter:
            stmt = stmt.where(User.status == status_filter)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "role": u.role,
                "status": u.status,
            }
            for u in rows
        ]


# approve / deny / ban / unban


async def _approve_async(username: str, admin: Any) -> Any:
    await bootstrap_if_needed()
    async with session_scope() as session:
        return await _approve_user(session, username=username, admin=admin)


async def _deny_async(username: str, admin: Any, reason: str | None) -> Any:
    await bootstrap_if_needed()
    async with session_scope() as session:
        return await _deny_user(session, username=username, admin=admin, reason=reason)


async def _ban_async(username: str, admin: Any, reason: str | None) -> Any:
    await bootstrap_if_needed()
    async with session_scope() as session:
        return await _ban_user(session, username=username, admin=admin, reason=reason)


async def _unban_async(username: str, admin: Any) -> Any:
    await bootstrap_if_needed()
    async with session_scope() as session:
        return await _unban_user(session, username=username, admin=admin)


# quota


async def _get_quota_async(username: str) -> dict[str, int]:
    await bootstrap_if_needed()
    from outo_models.db.session import session_scope
    from outo_models.repos.quota import ensure_quota_rows

    validate_slug(username)
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        quota, usage = await ensure_quota_rows(session, user)
        return {"max_bytes": int(quota.max_bytes), "used_bytes": int(usage.used_bytes)}


async def _set_quota_async(username: str, admin: Any, max_bytes: int) -> int:
    await bootstrap_if_needed()
    from outo_models.repos.quota import ensure_quota_rows

    validate_slug(username)
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        quota, _ = await ensure_quota_rows(session, user)
        old_value = int(quota.max_bytes)
        quota.max_bytes = max_bytes
        session.add(
            AuditLog(
                actor_id=admin.id,
                action="admin.quota",
                target_type="user",
                target_id=str(user.id),
                detail=json.dumps(
                    {"old_max_bytes": old_value, "new_max_bytes": max_bytes},
                    ensure_ascii=False,
                ),
            )
        )
        return max_bytes


# gpu


async def _get_gpu_async(username: str) -> list[str]:
    await bootstrap_if_needed()
    from outo_models.server.routers._admin_helpers import get_gpu_assignments

    validate_slug(username)
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        return list(await get_gpu_assignments(session, username))


async def _set_gpu_async(username: str, admin: Any, gpu_ids: list[str]) -> None:
    await bootstrap_if_needed()
    from outo_models.server.routers._admin_helpers import gpu_setting_key

    validate_slug(username)
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        key = gpu_setting_key(username)
        row = (
            await session.execute(select(WebSetting).where(WebSetting.key == key))
        ).scalar_one_or_none()
        encoded = json.dumps(list(gpu_ids), ensure_ascii=False)
        if row is None:
            session.add(WebSetting(key=key, value=encoded))
        else:
            row.value = encoded
        session.add(
            AuditLog(
                actor_id=admin.id,
                action="admin.gpu",
                target_type="user",
                target_id=str(user.id),
                detail=encoded,
            )
        )


async def _clear_gpu_async(username: str, admin: Any) -> None:
    await bootstrap_if_needed()
    from outo_models.server.routers._admin_helpers import gpu_setting_key

    validate_slug(username)
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        key = gpu_setting_key(username)
        row = (
            await session.execute(select(WebSetting).where(WebSetting.key == key))
        ).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
            session.add(
                AuditLog(
                    actor_id=admin.id,
                    action="admin.gpu",
                    target_type="user",
                    target_id=str(user.id),
                    detail=json.dumps({"cleared": True}, ensure_ascii=False),
                )
            )


# reset-password (local only)


async def _reset_password_async(username: str, admin: Any, new_password: str) -> Any:
    await bootstrap_if_needed()
    validate_slug(username)
    new_hash = hash_password(new_password)
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        user.password_hash = new_hash
        session.add(
            AuditLog(
                actor_id=admin.id,
                action="admin.reset_password",
                target_type="user",
                target_id=str(user.id),
                detail=None,
            )
        )
        return user


def reset_password(username: str, admin: Any, new_password: str) -> Any:
    return run_async(_reset_password_async(username, admin, new_password))


__all__ = [
    "require_admin_for_local",
    "run_async",
]
