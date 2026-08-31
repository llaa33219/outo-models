"""Spaces CRUD endpoints, mirroring the repos router.

Spaces are a `Repo(kind="space")` row plus an SDK sidecar file under
`spaces_dir/<owner>/<name>.json`. The router reads the sidecar on
every detail GET so the JSON shape stays current with whatever SDK the
operator last selected.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.db import Repo, User
from outo_models.exceptions import ForbiddenError, NotFoundError
from outo_models.repos.models import Visibility
from outo_models.server.deps import get_current_user, get_current_user_optional, get_db
from outo_models.spaces import (
    SUPPORTED_SDKS,
    create_space,
    delete_space,
    get_space,
    list_spaces,
    read_space_meta,
    runtime_status,
    update_space,
)
from outo_models.utils.git_url import clone_url
from outo_models.utils.slug import validate_slug

router = APIRouter(prefix="/api/spaces", tags=["spaces"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateSpaceRequest(BaseModel):
    """POST /api/spaces body."""

    name: str = Field(min_length=1, max_length=63)
    sdk: str = "static"
    visibility: Visibility = Visibility.PRIVATE
    description: str | None = Field(default=None, max_length=500)


class SpaceSummary(BaseModel):
    """Summary row used in list + create responses."""

    id: int
    name: str
    sdk: str
    visibility: str
    description: str | None
    owner: str
    clone_url: str
    created_at: str


class RuntimeBlock(BaseModel):
    """Embedded runtime-status block on the detail payload."""

    state: str
    message: str
    docs_url: str


class SpaceDetail(SpaceSummary):
    """Detail row used by `GET /api/spaces/{owner}/{name}`."""

    runtime: RuntimeBlock


class PatchSpaceRequest(BaseModel):
    """PATCH /api/spaces/{owner}/{name} body."""

    visibility: Visibility | None = None
    description: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(row: Repo) -> SpaceSummary:
    """Convert a `Repo` ORM row + on-disk SDK into the JSON summary."""
    sdk = read_space_meta(row.owner.username, row.name).sdk if row.owner else "static"
    return SpaceSummary(
        id=row.id,
        name=row.name,
        sdk=sdk,
        visibility=row.visibility,
        description=row.description,
        owner=row.owner.username if row.owner else "",
        clone_url=clone_url(row.owner.username, row.name) if row.owner else "",
        created_at=row.created_at.isoformat(),
    )


def _viewer_can_see(viewer: User | None, row: Repo) -> bool:
    """Public spaces: anyone. Private spaces: owner or admin only."""
    if row.visibility == Visibility.PUBLIC.value:
        return True
    if viewer is None:
        return False
    return viewer.id == row.owner_id or viewer.role == "admin"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=SpaceSummary, status_code=status.HTTP_201_CREATED)
async def create_space_route(
    body: CreateSpaceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SpaceSummary:
    """Create a Space row + sidecar; commit immediately."""
    if body.sdk not in SUPPORTED_SDKS:
        raise NotFoundError(f"unsupported sdk: {body.sdk!r}")
    repo = await create_space(
        db,
        owner=user,
        name=body.name,
        sdk=body.sdk,
        visibility=body.visibility,
        description=body.description,
    )
    await db.commit()
    await db.refresh(repo)
    reloaded = (
        await db.execute(
            select(Repo)
            .where(Repo.id == repo.id)
            .options(selectinload(Repo.owner))
        )
    ).scalar_one()
    return _summary(reloaded)


@router.get("", response_model=list[SpaceSummary])
async def list_spaces_route(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    owner: str | None = Query(default=None),
) -> list[SpaceSummary]:
    """List Spaces; owner filter narrows the result, visibility filters public.

    Anonymous / non-owner callers only see public spaces; if `owner` is
    supplied and matches `viewer.username`, private spaces of that owner
    are included.
    """
    is_admin = viewer is not None and viewer.role == "admin"
    include_private = False
    if owner is not None:
        validate_slug(owner)
        include_private = is_admin or (
            viewer is not None and viewer.username == owner
        )
    rows = await list_spaces(
        db,
        owner_name=owner,
        include_private=include_private,
    )
    # Eager-load owners for the response (the domain helper leaves them lazy).
    if rows:
        ids = [r.id for r in rows]
        owners = {
            row.id: row
            for row in (
                await db.execute(
                    select(Repo)
                    .where(Repo.id.in_(ids))
                    .options(selectinload(Repo.owner))
                )
            ).scalars().all()
        }
        for row in rows:
            fresh = owners.get(row.id)
            if fresh is not None:
                row.owner = fresh.owner
    return [_summary(r) for r in rows]


@router.get("/{owner}/{name}", response_model=SpaceDetail)
async def get_space_route(
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> SpaceDetail:
    """Return the space + on-disk SDK + runtime status (v1 stub)."""
    repo = await get_space(db, owner_name=owner, name=name)
    if not _viewer_can_see(viewer, repo):
        raise NotFoundError(f"space not found: {owner}/{name}")
    sdk = read_space_meta(owner, name).sdk
    status_ = runtime_status(repo)
    return SpaceDetail(
        id=repo.id,
        name=repo.name,
        sdk=sdk,
        visibility=repo.visibility,
        description=repo.description,
        owner=repo.owner.username if repo.owner else owner,
        clone_url=clone_url(repo.owner.username, repo.name) if repo.owner else "",
        created_at=repo.created_at.isoformat(),
        runtime=RuntimeBlock(
            state=status_.state.value,
            message=status_.message,
            docs_url=status_.docs_url,
        ),
    )


@router.patch("/{owner}/{name}", response_model=SpaceSummary)
async def patch_space_route(
    owner: str,
    name: str,
    body: PatchSpaceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SpaceSummary:
    """Update visibility / description (owner or admin). SDK is immutable in v1."""
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may modify this space")
    updated = await update_space(
        db,
        space=repo,
        visibility=body.visibility,
        description=body.description,
    )
    await db.commit()
    await db.refresh(updated)
    return _summary(updated)


@router.delete("/{owner}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space_route(
    owner: str,
    name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Hard-delete a space (owner or admin)."""
    validate_slug(owner)
    validate_slug(name)
    target_user = (
        await db.execute(select(User).where(User.username == owner))
    ).scalar_one_or_none()
    if target_user is None:
        raise NotFoundError(f"user {owner!r} not found")
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may delete this space")
    await delete_space(db, owner=target_user, name=name)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
