"""Delete a repository.

`delete_repo` removes both the on-disk bare repo and every DB row that
references it, in the correct order so a crash mid-delete leaves the
system either fully intact or trivially recoverable.
"""

from __future__ import annotations

import json
import shutil

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.db import AuditLog, Repo, Revision, User
from outo_models.exceptions import NotFoundError
from outo_models.repos.models import RepoKind
from outo_models.repos.quota import add_usage
from outo_models.repos.storage import REPO_LOCKS, repo_fs_path


async def delete_repo(
    session: AsyncSession,
    *,
    owner: User,
    name: str,
    kind: RepoKind,
) -> None:
    """Delete a repo: revisions, row, audit entry, on-disk dir, and usage.

    Steps (in order):
        1. Locate the `Repo` row; raise `NotFoundError` if absent.
        2. Hold the per-repo write lock so no concurrent push or create can
           interleave with the on-disk removal.
        3. Delete every `Revision` row for this repo, then the `Repo` row.
        4. Append a `repo.delete` audit entry.
        5. Decrement `UserUsage.used_bytes` by the previously recorded size,
           clamped at zero.
        6. Remove the on-disk directory (`ignore_errors=True` so a missing
           dir is not an error).

    The session is NOT committed — routers own the transaction. All DB
    mutations are flushed before return so a unique-constraint or FK
    failure surfaces immediately rather than at commit time.
    """
    repo_row = (
        await session.execute(
            select(Repo).where(
                Repo.owner_id == owner.id,
                Repo.kind == kind.value,
                Repo.name == name,
            )
        )
    ).scalar_one_or_none()
    if repo_row is None:
        raise NotFoundError(f"repository not found: {owner.username}/{name} ({kind.value})")

    fs_path = repo_fs_path(owner.username, name)
    size_to_release = repo_row.size_bytes

    async with REPO_LOCKS.acquire(owner.username, name):
        await session.execute(delete(Revision).where(Revision.repo_id == repo_row.id))
        await session.delete(repo_row)

        session.add(
            AuditLog(
                actor_id=owner.id,
                action="repo.delete",
                target_type="repo",
                detail=json.dumps({"name": name, "kind": kind.value}),
            )
        )

        # Decrement usage in the same transaction so the two operations
        # commit atomically. `add_usage` clamps at zero, so the floor rule
        # in the contract is enforced there.
        await add_usage(session, owner, -size_to_release)

        await session.flush()

        # On-disk removal runs LAST inside the lock; `ignore_errors=True`
        # makes the operation idempotent (a missing dir is not an error,
        # which is exactly what we want for crash-recovery replay).
        shutil.rmtree(fs_path, ignore_errors=True)


__all__ = ["delete_repo"]
