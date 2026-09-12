"""User-facing read-only endpoints.

Public profile + repos; visibility rules apply. Anonymous callers see
public repos only; the profile owner and any admin sees private repos too.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.db import AuditLog, Repo, User
from outo_models.exceptions import NotFoundError, ValidationFailedError
from outo_models.repos.models import Visibility
from outo_models.repos.social import (
    follow_user,
    follower_count,
    is_following,
    load_user_or_404,
    unfollow_user,
)
from outo_models.server.deps import (
    get_current_user,
    get_current_user_optional,
    get_db,
)
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
    links: list[dict[str, str]] = []
    try:
        parsed = json.loads(user.links or "[]")
        if isinstance(parsed, list):
            links = [x for x in parsed if isinstance(x, dict)]
    except json.JSONDecodeError:
        links = []
    interests: list[str] = []
    try:
        parsed_i = json.loads(user.interests or "[]")
        if isinstance(parsed_i, list):
            interests = [str(x) for x in parsed_i]
    except json.JSONDecodeError:
        interests = []
    return {
        "username": user.username,
        "display_name": user.display_name,
        "bio": user.bio,
        "interests": interests,
        "links": links,
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


@router.post(
    "/{username}/follow",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def follow_user_route(
    username: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> dict[str, object]:
    """Follow `username` (auth required; idempotent).

    404 if the target user does not exist; 403 if the caller is trying
    to follow themselves (DB-level CHECK + service-layer guard).
    """
    target = await load_user_or_404(db, username=username)
    inserted = await follow_user(db, follower=user, followee=target)
    await db.commit()
    response.status_code = status.HTTP_201_CREATED if inserted else status.HTTP_200_OK
    return {
        "following": True,
        "follower_count": await follower_count(db, followee=target),
    }


@router.delete(
    "/{username}/follow",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unfollow_user_route(
    username: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Unfollow `username` (auth required; idempotent)."""
    target = await load_user_or_404(db, username=username)
    await unfollow_user(db, follower=user, followee=target)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{username}/follow", response_model=None)
async def get_follow_state_route(
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> dict[str, object]:
    """Return `following` (caller's view) and `follower_count` for `username`."""
    target = await load_user_or_404(db, username=username)
    following = (
        await is_following(db, follower=viewer, followee=target) if viewer is not None else False
    )
    return {
        "following": following,
        "follower_count": await follower_count(db, followee=target),
    }


__all__ = ["router"]


class UpdateProfileRequest(BaseModel):
    """POST /api/users/me/profile body — all fields optional."""

    display_name: str | None = Field(default=None, max_length=80)
    bio: str | None = Field(default=None, max_length=2000)
    interests: list[str] | None = None
    links: list[dict[str, str]] | None = None


def _validate_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Normalize + validate external links (http/https only)."""
    if len(links) > 8:
        raise ValidationFailedError("at most 8 links allowed")
    cleaned: list[dict[str, str]] = []
    for item in links:
        label = str(item.get("label", "")).strip()
        url = str(item.get("url", "")).strip()
        if not label or len(label) > 40:
            raise ValidationFailedError("each link needs a label of 1-40 chars")
        if not url.startswith(("http://", "https://")):
            raise ValidationFailedError(f"link {label!r} must start with http:// or https://")
        if len(url) > 500:
            raise ValidationFailedError("link URL too long")
        cleaned.append({"label": label, "url": url})
    return cleaned


def _validate_interests(interests: list[str]) -> list[str]:
    if len(interests) > 12:
        raise ValidationFailedError("at most 12 interests allowed")
    cleaned: list[str] = []
    for raw in interests:
        tag = str(raw).strip().lower()
        if not tag or len(tag) > 24 or not all(c.isalnum() or c in "-_" for c in tag):
            raise ValidationFailedError(
                f"invalid interest tag {raw!r} (letters/digits/-/_ up to 24 chars)"
            )
        cleaned.append(tag)
    return cleaned


@router.post("/me/profile", response_model=None)
async def update_own_profile(
    body: UpdateProfileRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """Update the caller's public profile surface (display_name, bio,
    AI/ML interest tags, external links). Route sits before /{username} so
    `me` is never mistaken for a username."""
    if body.display_name is not None:
        user.display_name = body.display_name.strip() or None
    if body.bio is not None:
        user.bio = body.bio.strip() or None
    if body.interests is not None:
        user.interests = json.dumps(_validate_interests(body.interests)) if body.interests else None
    if body.links is not None:
        user.links = json.dumps(_validate_links(body.links)) if body.links else None
    db.add(
        AuditLog(
            actor_id=user.id,
            action="user.profile_update",
            target_type="user",
            target_id=str(user.id),
        )
    )
    await db.commit()
    return {"ok": True}
