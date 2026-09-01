"""User-facing read-only endpoints.

Public profile + repos; visibility rules apply. Anonymous callers see
public repos only; the profile owner and any admin sees private repos too.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.db import Repo, User
from outo_models.exceptions import NotFoundError
from outo_models.repos.models import Visibility
from outo_models.server.deps import get_current_user_optional, get_db
from outo_models.utils.slug import validate_slug

router = APIRouter(prefix="/api/users", tags=["users"])


async def _load_user(db: AsyncSession, username: str) -> User:
    """Fetch the user by slug or raise `NotFoundError`."""
    validate_slug(username)
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"user {username!r} not found")
    return user


def _user_can_see_private(viewer: User | None, target: User) -> bool:
    """`True` when `viewer` may see `target`'s private repos."""
    if viewer is None:
        return False
    return viewer.id == target.id or viewer.role == "admin"


@router.get("/{username}")
async def get_profile(
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> dict[str, object]:
    """Public profile JSON for `username`.

    Visibility: the endpoint itself never gates — user profiles are public
    information (username, display_name, created_at). Private repo
    details are returned by `GET /api/users/{username}/repos`, where the
    visibility rule lives.
    """
    user = await _load_user(db, username)
    repo_count = (
        await db.execute(
            select(func.count(Repo.id)).where(
                Repo.owner_id == user.id,
                Repo.visibility == Visibility.PUBLIC.value,
            )
        )
    ).scalar_one()
    return {
        "username": user.username,
        "display_name": user.display_name,
        "created_at": user.created_at.isoformat(),
        "public_repo_count": int(repo_count or 0),
    }


@router.get("/{username}/repos")
async def list_user_repos(
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> list[dict[str, object]]:
    """List repos owned by `username`.

    Visibility rules:
        * Anonymous / other user → public repos only.
        * Owner or admin → every repo (public + private).
    """
    user = await _load_user(db, username)
    stmt = select(Repo).where(Repo.owner_id == user.id).order_by(Repo.id)
    if not _user_can_see_private(viewer, user):
        stmt = stmt.where(Repo.visibility == Visibility.PUBLIC.value)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "visibility": row.visibility,
            "description": row.description,
            "size_bytes": row.size_bytes,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


# Suppress unused-status import warning on the wildcard re-export.
_ = status
__all__ = ["router"]
