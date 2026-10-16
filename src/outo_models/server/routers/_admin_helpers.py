"""Shared admin helpers (storage + audit + quota summaries).

Split out from `admin.py` so each top-level function in that router stays
small enough to read. Pure domain functions: no FastAPI imports, no
dependency wiring — they take an `AsyncSession` (the routers commit).

The GPU assignment storage key is namespaced as `gpu:<username>` so an
operator-scoped setting (e.g. `gpu:alice`) cannot collide with a future
namespace (e.g. `disk:alice`). The round-trip is JSON-encoded to keep
the `web_settings.value` column portable.

The three mutations (`set_user_quota`, `set_user_gpu_ids`,
`clear_user_gpu_ids`) live here, not in `admin.py`, so the JSON API and
the HTML admin dashboard share one implementation: same validation, same
`AuditLog` rows, same storage layout. Callers commit.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.db import AuditLog, User, WebSetting
from outo_models.exceptions import NotFoundError, ValidationFailedError
from outo_models.repos.quota import ensure_quota_rows
from outo_models.utils.slug import validate_slug

#: Upper bound for a per-user storage quota (1 PB). Mirrors the REST
#: body's `Field(le=10**15)` so both surfaces reject the same values.
MAX_QUOTA_BYTES = 10**15

#: Hard cap of `web_settings.value` (`String(2000)`). An over-long GPU
#: list is rejected rather than silently truncated by the database.
MAX_GPU_VALUE_CHARS = 2000

#: GPU ids arrive from operators as a free-text list; commas, spaces,
#: tabs and newlines are all accepted as separators.
_GPU_ID_SEPARATOR = re.compile(r"[,\s]+")


def gpu_setting_key(username: str) -> str:
    """Storage key under `web_settings.key` for a user's GPU assignments."""
    return f"gpu:{username}"


async def get_gpu_assignments(db: AsyncSession, username: str) -> list[str]:
    """Return the GPU ids currently assigned to `username`.

    A missing row → `[]`. Robustness over the wire is intentional: the
    field is operator-controlled, so a typo must not crash the admin
    endpoint.
    """
    row = (
        await db.execute(select(WebSetting).where(WebSetting.key == gpu_setting_key(username)))
    ).scalar_one_or_none()
    return [] if row is None else decode_gpu_ids(row.value)


def decode_gpu_ids(value: str | None) -> list[str]:
    """Decode a stored GPU value into an id list.

    Malformed JSON → `[]`; a non-list value → `[]`. Shared by the
    single-user REST read and the dashboard's batched table read so the
    two surfaces can never disagree about what a row holds.
    """
    if not value:
        return []
    try:
        decoded = json.loads(value)
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
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
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


def parse_gpu_ids(raw: str) -> list[str]:
    """Split operator input into a de-duplicated GPU id list.

    Accepts commas, spaces, tabs and newlines as separators, drops empty
    entries, and keeps first-seen order. An empty result means "no
    assignments" — the caller clears instead of writing.
    """
    return list(dict.fromkeys(part for part in _GPU_ID_SEPARATOR.split(raw.strip()) if part))


def _encode_gpu_ids(gpu_ids: list[str]) -> str:
    """JSON-encode a GPU id list, enforcing the column's length cap."""
    encoded = json.dumps(list(gpu_ids), ensure_ascii=False)
    if len(encoded) > MAX_GPU_VALUE_CHARS:
        raise ValidationFailedError(
            f"gpu assignment list is too long "
            f"({len(encoded)} characters; the limit is {MAX_GPU_VALUE_CHARS})"
        )
    return encoded


async def set_user_quota(db: AsyncSession, user: User, max_bytes: int, *, actor: User) -> None:
    """Set `user`'s storage quota and audit the change (caller commits).

    Validates `0 < max_bytes <= MAX_QUOTA_BYTES` BEFORE touching any row so
    a rejected value leaves no partial write behind.
    """
    if max_bytes <= 0 or max_bytes > MAX_QUOTA_BYTES:
        raise ValidationFailedError(
            f"quota must be between 1 and {MAX_QUOTA_BYTES} bytes (got {max_bytes})"
        )
    quota, _usage = await ensure_quota_rows(db, user)
    old_value = quota.max_bytes
    quota.max_bytes = max_bytes
    write_admin_audit(
        db,
        admin=actor,
        action="admin.quota",
        target_id=user.id,
        detail=json.dumps(
            {"old_max_bytes": old_value, "new_max_bytes": max_bytes},
            ensure_ascii=False,
        ),
    )


async def set_user_gpu_ids(
    db: AsyncSession, user: User, gpu_ids: list[str], *, actor: User
) -> None:
    """Replace `user`'s GPU assignment list and audit it (caller commits)."""
    encoded = _encode_gpu_ids(gpu_ids)
    key = gpu_setting_key(user.username)
    row = (await db.execute(select(WebSetting).where(WebSetting.key == key))).scalar_one_or_none()
    if row is None:
        db.add(WebSetting(key=key, value=encoded))
    else:
        row.value = encoded
    write_admin_audit(
        db,
        admin=actor,
        action="admin.gpu",
        target_id=user.id,
        detail=encoded,
    )


async def clear_user_gpu_ids(db: AsyncSession, user: User, *, actor: User) -> None:
    """Drop `user`'s GPU assignment row entirely (caller commits).

    A no-op when there is nothing stored — no audit row is written for a
    clear that had nothing to clear.
    """
    key = gpu_setting_key(user.username)
    row = (await db.execute(select(WebSetting).where(WebSetting.key == key))).scalar_one_or_none()
    if row is None:
        return
    await db.delete(row)
    write_admin_audit(
        db,
        admin=actor,
        action="admin.gpu",
        target_id=user.id,
        detail=json.dumps({"cleared": True}),
    )


__all__ = [
    "MAX_QUOTA_BYTES",
    "clear_user_gpu_ids",
    "decode_gpu_ids",
    "get_gpu_assignments",
    "gpu_setting_key",
    "load_target_user",
    "parse_gpu_ids",
    "quota_dict",
    "set_user_gpu_ids",
    "set_user_quota",
    "write_admin_audit",
]
