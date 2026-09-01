"""Per-user storage quota + usage tracking.

`UserQuota` carries the operator-assigned cap; `UserUsage` carries the
current byte count. The two are intentionally separated so the cap can be
updated independently of the (relatively expensive) reconciliation pass.

All functions take an `AsyncSession` and DO NOT commit. Routers own
transactions; this module never touches `session.commit()` / `rollback()`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.config import get_settings
from outo_models.db import Repo, User, UserQuota, UserUsage
from outo_models.exceptions import QuotaExceededError
from outo_models.repos.storage import disk_usage, repo_fs_path


async def ensure_quota_rows(session: AsyncSession, user: User) -> tuple[UserQuota, UserUsage]:
    """Make sure `user` has both a `UserQuota` and a `UserUsage` row.

    Idempotent: existing rows are returned unchanged; missing rows are
    inserted with `UserQuota.max_bytes = settings.default_quota_bytes` and
    `UserUsage.used_bytes = 0`. Returns the live rows so callers can read the
    resolved values without an extra SELECT.
    """
    quota = (
        await session.execute(select(UserQuota).where(UserQuota.user_id == user.id))
    ).scalar_one_or_none()
    if quota is None:
        quota = UserQuota(user_id=user.id, max_bytes=get_settings().default_quota_bytes)
        session.add(quota)
        await session.flush()

    usage = (
        await session.execute(select(UserUsage).where(UserUsage.user_id == user.id))
    ).scalar_one_or_none()
    if usage is None:
        usage = UserUsage(user_id=user.id, used_bytes=0)
        session.add(usage)
        await session.flush()

    return quota, usage


async def check_push_allowed(session: AsyncSession, user: User, incoming_bytes: int) -> None:
    """Raise `QuotaExceededError` if `used + incoming` would exceed the cap.

    `incoming_bytes` is the bytes-about-to-be-added by the push; negative
    values (forced removals) always pass because freeing space is never a
    quota violation. Materializes quota/usage rows lazily so a brand-new
    account can be checked without first calling `ensure_quota_rows`.
    """
    if incoming_bytes <= 0:
        return
    quota, usage = await ensure_quota_rows(session, user)
    if usage.used_bytes + incoming_bytes > quota.max_bytes:
        raise QuotaExceededError(
            f"quota exceeded: used={usage.used_bytes} "
            f"+ incoming={incoming_bytes} > max={quota.max_bytes}"
        )


async def add_usage(session: AsyncSession, user: User, delta_bytes: int) -> None:
    """Adjust the user's `used_bytes` by `delta_bytes`, clamped at zero.

    A negative `delta_bytes` shrinks the tally; the result is clamped at 0
    so transient drift (e.g. between two reconcile passes) cannot leave a
    negative byte count. Materializes the row if missing.
    """
    _, usage = await ensure_quota_rows(session, user)
    new_value = usage.used_bytes + delta_bytes
    usage.used_bytes = max(0, new_value)
    await session.flush()


async def reconcile_user(session: AsyncSession, user: User) -> int:
    """Recompute `UserUsage.used_bytes` from the on-disk truth.

    Walks every repo owned by `user`, sums `disk_usage(path)`, writes the sum
    back to `UserUsage`, and returns `new_used - old_used` so the caller can
    log the drift (negative or positive). Materializes the usage row if
    missing — every account should have a tally, even one that owns zero
    repos.
    """
    old_used = 0
    existing_usage = (
        await session.execute(select(UserUsage).where(UserUsage.user_id == user.id))
    ).scalar_one_or_none()
    if existing_usage is not None:
        old_used = existing_usage.used_bytes

    rows = (
        await session.execute(select(Repo.name, Repo.kind).where(Repo.owner_id == user.id))
    ).all()

    total = 0
    for name, _kind in rows:
        total += await disk_usage(repo_fs_path(user.username, name))

    if existing_usage is None:
        session.add(UserUsage(user_id=user.id, used_bytes=total))
    else:
        existing_usage.used_bytes = total
    await session.flush()

    return total - old_used


__all__ = [
    "add_usage",
    "check_push_allowed",
    "ensure_quota_rows",
    "reconcile_user",
]
