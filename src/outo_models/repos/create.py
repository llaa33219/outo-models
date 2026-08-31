"""Create a new bare repository.

`create_repo` is the single entry point used by routers to materialize a
new repo on disk and in the database. It owns the on-disk / DB write order
and the compensating cleanup, so callers only need to wrap it in a single
transaction.
"""

from __future__ import annotations

import json
import shutil

from dulwich import porcelain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.db import AuditLog, Repo, User
from outo_models.exceptions import ConflictError
from outo_models.repos.models import RepoKind, Visibility
from outo_models.repos.quota import ensure_quota_rows
from outo_models.repos.storage import REPO_LOCKS, repo_fs_path
from outo_models.utils import repos_dir, validate_slug


async def create_repo(
    session: AsyncSession,
    *,
    owner: User,
    name: str,
    kind: RepoKind,
    visibility: Visibility = Visibility.PRIVATE,
    description: str | None = None,
) -> Repo:
    """Create a bare repo on disk and a matching `Repo` row.

    Steps (in order):
        1. Validate the name slug.
        2. Reject duplicates with `ConflictError`.
        3. Hold the per-repo write lock to serialize concurrent writers.
        4. `mkdir -p` the owner segment and `dulwich.porcelain.init` a bare
           repository at the on-disk path.
        5. Insert the `Repo` row with the relative path, ensure quota rows,
           and append a `repo.create` audit entry.
        6. If any DB operation fails after step 4, remove the on-disk repo
           before re-raising.

    The session is NOT committed — routers own the transaction. All rows
    added by this function are flushed before return so the caller can
    detect unique-constraint violations immediately.
    """
    validate_slug(name)

    existing = (
        await session.execute(
            select(Repo.id).where(
                Repo.owner_id == owner.id,
                Repo.kind == kind.value,
                Repo.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            f"repository already exists: {owner.username}/{name} ({kind.value})"
        )

    fs_path = repo_fs_path(owner.username, name)
    async with REPO_LOCKS.acquire(owner.username, name):
        # Step 4: materialize the bare repo. Failures here are NOT cleaned up
        # — there is nothing to clean up yet.
        fs_path.parent.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(fs_path), bare=True)

        # Step 5: anything past this point must roll back the on-disk repo
        # if the DB write fails, so the system does not accumulate orphan
        # bare repos that no row references.
        relative_path = fs_path.relative_to(repos_dir()).as_posix()
        try:
            repo = Repo(
                owner_id=owner.id,
                name=name,
                kind=kind.value,
                visibility=visibility.value,
                description=description,
                default_branch="main",
                size_bytes=0,
                path=relative_path,
            )
            session.add(repo)

            await ensure_quota_rows(session, owner)

            session.add(
                AuditLog(
                    actor_id=owner.id,
                    action="repo.create",
                    target_type="repo",
                    detail=json.dumps({"name": name, "kind": kind.value}),
                )
            )

            await session.flush()
        except Exception:
            # Compensating cleanup: drop the freshly-created bare repo so the
            # next call can re-attempt the create. `ignore_errors=True` so a
            # removal failure cannot mask the original exception.
            shutil.rmtree(fs_path, ignore_errors=True)
            raise

    return repo


__all__ = ["create_repo"]
