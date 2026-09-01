"""Scheduled audit-log retention prune.

`prune_audit_logs` deletes every `AuditLog` row whose `created_at` is older
than `retention_days`. It is wired into the scheduler as the `audit_prune`
job — daily, off-peak — and the scheduler expects it to never raise. The
two injectable hooks (`now` and `session_factory`) exist so tests can
own the wall clock and the DB session without spinning up a fake
SQLAlchemy engine.

Design notes:

* Default `session_factory=session_scope` — the same context manager the
  other one-off scripts use. Callers that already hold an open session
  (rare; the scheduler runs detached) can inject a custom factory.
* Default `now=datetime.now(UTC)` — the production code never injects a
  clock. Tests inject `now` to lock the cutoff.
* Boundary semantics: a row whose `created_at == now - retention_days` is
  retained (strict less-than). Operators usually think "older than 90 days"
  as "at least 90 days ago", which is what `<` delivers.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult

from outo_models.db.models import AuditLog
from outo_models.db.session import session_scope

# Module logger so the production JSON pipeline can grep `outo_models.tasks.jobs.audit_prune`.
_logger: Any = __import__("structlog").get_logger("outo_models.tasks.jobs.audit_prune")

# Default retention — operators can override via the future admin UI; for now
# the scheduler hardcodes 90 days and the parameter exists for tests / config.
_DEFAULT_RETENTION_DAYS = 90


async def prune_audit_logs(
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    *,
    now: dt.datetime | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> int:
    """Delete `AuditLog` rows older than `retention_days`; return the deleted count.

    Args:
        retention_days: Rows whose `created_at` is strictly less than
            `now - retention_days` are deleted. Must be non-negative.
        now: Reference timestamp. Defaults to `datetime.now(UTC)` so the
            scheduler gets a fresh wall-clock each run.
        session_factory: Zero-arg callable that returns an async context
            manager yielding a usable `AsyncSession`. Defaults to
            `outo_models.db.session.session_scope`. Tests inject a per-test
            factory so they can observe the DB after the prune runs.

    Returns:
        The number of rows deleted. Zero when nothing is stale.

    Raises:
        ValueError: When `retention_days < 0` — a negative window is a
            caller bug, not a transient runtime condition, and the
            scheduler must not silently swallow a misconfiguration.
    """
    if retention_days < 0:
        raise ValueError(f"retention_days must be >= 0, got {retention_days}")

    current = now if now is not None else dt.datetime.now(dt.UTC)
    cutoff = current - dt.timedelta(days=retention_days)
    factory = session_scope if session_factory is None else session_factory

    async with factory() as session:
        result = await session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        # `session.execute` is typed as `Result[Any]`, but a Core DELETE
        # actually returns a `CursorResult`, which carries `rowcount`.
        cursor = cast(CursorResult[Any], result)
        deleted = int(cursor.rowcount or 0)
        # `session_scope` auto-commits, but an injected `async_sessionmaker` does not.
        await session.commit()

    _logger.info(
        "audit_prune completed",
        retention_days=retention_days,
        deleted=deleted,
        cutoff=cutoff.isoformat(),
    )
    return deleted


__all__ = ["prune_audit_logs"]
