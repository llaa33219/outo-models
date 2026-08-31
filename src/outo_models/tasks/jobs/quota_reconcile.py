"""Scheduled quota-reconcile job body.

`quota_reconcile_job` runs hourly and asks WP-8's
`outo_models.repos.quota.reconcile_user(session, user)` to recompute every
user's storage usage from disk. Because WP-8 ships the quota package in
parallel with this module, the import is deferred to call time and a
missing module is treated as "not ready yet" — we log a warning and skip
this tick rather than crashing the scheduler.

The job never raises. A user whose reconcile explodes is logged and the
loop continues with the next user; a DB failure is logged and the entire
tick is abandoned until the next hour.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from outo_models.db.models import User
from outo_models.db.session import session_scope

_logger = structlog.get_logger("outo_models.tasks.jobs.quota_reconcile")


async def quota_reconcile_job() -> None:
    """Recompute every user's storage usage against the disk.

    Lazy-imports `outo_models.repos.quota.reconcile_user` so this module
    is independently testable while WP-8 lands. If the package is absent
    (e.g. the operator hasn't run `update` yet), the tick is a no-op with
    a warning — the next hourly run gets another chance.
    """
    try:
        from outo_models.repos.quota import reconcile_user
    except ImportError as exc:
        _logger.warning(
            "quota_reconcile_job skipped: reconcile_user not available",
            error=str(exc),
        )
        return

    try:
        async with session_scope() as session:
            users = (await session.execute(select(User))).scalars().all()
            for user in users:
                try:
                    await reconcile_user(session, user)
                except Exception as exc:
                    _logger.warning(
                        "quota reconcile_user raised; continuing with next user",
                        user_id=user.id,
                        username=user.username,
                        error=str(exc),
                    )
    except Exception as exc:
        _logger.warning(
            "quota_reconcile_job raised; swallowing to keep scheduler alive",
            error=str(exc),
        )
        return

    _logger.info("quota_reconcile_job completed")


__all__ = ["quota_reconcile_job"]
