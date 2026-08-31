"""Shared admin helpers (storage + audit + quota summaries).

Split out from `admin.py` so each top-level function in that router stays
small enough to read. Pure domain functions: no FastAPI imports, no
dependency wiring — they take an `AsyncSession` (the routers commit).

The GPU assignment storage key is namespaced as `gpu:<username>` so an
operator-scoped setting (e.g. `gpu:alice`) cannot collide with a future
namespace (e.g. `disk:alice`). The round-trip is JSON-encoded to keep
the `web_settings.value` column portable.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.db import AuditLog, User, WebSetting
from outo_models.exceptions import NotFoundError
from outo_models.repos.quota import ensure_quota_rows
from outo_models.utils.slug import validate_slug


def gpu_setting_key(username: str) -> str:
    """Storage key under `web_settings.key` for a user's GPU assignments."""
    return f"gpu:{username}"


async def get_gpu_assignments(db: AsyncSession, username: str) -> list[str]:
    """Return the GPU ids currently assigned to `username`.

    A missing row → `[]`; a malformed JSON value → `[]`; a non-list value
    → `[]`. Robustness over the wire is intentional: the field is
    operator-controlled, so a typo must not crash the admin endpoint.
    """
    row = (
        await db.execute(
            select(WebSetting).where(WebSetting.key == gpu_setting_key(username))
        )
    ).scalar_one_or_none()
    if row is None:
        return []
    try:
        decoded = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [s for s in decoded if isinstance(s, str)]


def write_admin_audit(
    db: AsyncSession,
    *,
    admin: User,
    action: str,
    target_id: int,
    detail: str | None = None,
) -> None:
    """Insert an `AuditLog` row owned by `admin` (caller commits)."""
    db.add(
        AuditLog(
            actor_id=admin.id,
            action=action,
            target_type="user",
            target_id=str(target_id),
            detail=detail,
        )
    )


async def load_target_user(db: AsyncSession, username: str) -> User:
    """Fetch the target user or raise `NotFoundError`."""
    validate_slug(username)
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"user {username!r} not found")
    return user


async def quota_dict(db: AsyncSession, user: User) -> dict[str, int]:
    """Return `{max_bytes, used_bytes}` for `user`, materializing rows."""
    quota, usage = await ensure_quota_rows(db, user)
    return {
        "max_bytes": quota.max_bytes,
        "used_bytes": usage.used_bytes,
    }


__all__ = [
    "get_gpu_assignments",
    "gpu_setting_key",
    "load_target_user",
    "quota_dict",
    "write_admin_audit",
]
