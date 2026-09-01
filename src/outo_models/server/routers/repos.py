"""Repo CRUD over the public REST API.

Authorization:
    * `GET` is open; visibility rules apply (anonymous → public only).
    * `POST` requires authentication.
    * `PATCH` / `DELETE` are owner-or-admin only.

The router owns its transactions: every mutation calls `db.commit()` so
the on-disk bare-repo / DB row change together. Domain modules in
`outo_models.repos` are responsible for atomicity *within* the call —
they never commit themselves.
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
from outo_models.repos.create import create_repo
from outo_models.repos.delete import delete_repo
from outo_models.repos.models import RepoKind, Visibility
from outo_models.repos.reflog import recent_revisions
from outo_models.server.deps import get_current_user, get_current_user_optional, get_db
from outo_models.utils.git_url import clone_url
from outo_models.utils.slug import validate_slug

router = APIRouter(prefix="/api/repos", tags=["repos"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateRepoRequest(BaseModel):
    """POST /api/repos body."""

    name: str = Field(min_length=1, max_length=63)
    kind: RepoKind = RepoKind.MODEL
    visibility: Visibility = Visibility.PRIVATE
    description: str | None = Field(default=None, max_length=500)


class RepoSummary(BaseModel):
    """Summary row used in list + create responses."""

    id: int
    name: str
    kind: str
    visibility: str
    description: str | None
    size_bytes: int
    owner: str
    clone_url: str
    created_at: str


class RepoDetail(RepoSummary):
    """Detail row used by `GET /api/repos/{owner}/{name}`."""

    default_branch: str
    recent_revisions: list[dict[str, object]]


class PatchRepoRequest(BaseModel):
    """PATCH /api/repos/{owner}/{name} body."""

    visibility: Visibility | None = None
    description: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(row: Repo) -> RepoSummary:
    """Convert a `Repo` ORM row to the JSON summary."""
    return RepoSummary(
        id=row.id,
        name=row.name,
        kind=row.kind,
        visibility=row.visibility,
        description=row.description,
        size_bytes=row.size_bytes,
        owner=row.owner.username if row.owner is not None else "",
        clone_url=clone_url(row.owner.username, row.name) if row.owner else "",
        created_at=row.created_at.isoformat(),
    )


async def _load_repo(db: AsyncSession, *, owner: str, name: str) -> Repo:
    """Fetch a repo by `(owner, name)` or raise `NotFoundError`."""
    return (
        await db.execute(
            select(Repo)
            .where(Repo.name == name)
            .options(selectinload(Repo.owner))
            .join(Repo.owner)
            .where(User.username == owner)
        )
    ).scalar_one_or_none() or _missing(owner, name)


def _missing(owner: str, name: str) -> Repo:
    """Sentinel helper so `_load_repo` can express the not-found case."""
    raise NotFoundError(f"repository not found: {owner}/{name}")


def _viewer_can_see(viewer: User | None, row: Repo) -> bool:
    """`True` if `viewer` is allowed to see `row` per visibility rules."""
    if row.visibility == Visibility.PUBLIC.value:
        return True
    if viewer is None:
        return False
    return viewer.id == row.owner_id or viewer.role == "admin"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=RepoSummary, status_code=status.HTTP_201_CREATED)
async def create_repo_route(
    body: CreateRepoRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSummary:
    """Create a new repository on disk + DB."""
    repo = await create_repo(
        db,
        owner=user,
        name=body.name,
        kind=body.kind,
        visibility=body.visibility,
        description=body.description,
    )
    await db.commit()
    await db.refresh(repo)
    # Re-load with the owner relationship populated for the summary.
    repo = (
        await db.execute(select(Repo).where(Repo.id == repo.id).options(selectinload(Repo.owner)))
    ).scalar_one()
    return _summary(repo)


@router.get("", response_model=list[RepoSummary])
async def list_repos(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    kind: RepoKind | None = Query(default=None),  # noqa: B008
    owner: str | None = Query(default=None),
) -> list[RepoSummary]:
    """List repos; anonymous callers see public ones only.

    Filters: `kind` (model/dataset), `owner` (username). When the caller
    is the owner of the queried username, private repos of that owner are
    also included. Admin sees everything regardless of owner filter.
    """
    stmt = select(Repo).options(selectinload(Repo.owner)).order_by(Repo.id)
    if kind is not None:
        stmt = stmt.where(Repo.kind == kind.value)
    if owner is not None:
        validate_slug(owner)
        stmt = stmt.join(Repo.owner).where(User.username == owner)

    is_admin = viewer is not None and viewer.role == "admin"
    is_owner_of_query = viewer is not None and owner is not None and viewer.username == owner
    if not (is_admin or is_owner_of_query):
        stmt = stmt.where(Repo.visibility == Visibility.PUBLIC.value)

    rows = (await db.execute(stmt)).scalars().all()
    return [_summary(r) for r in rows]


@router.get("/{owner}/{name}", response_model=RepoDetail)
async def get_repo(
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> RepoDetail:
    """Return repo metadata + recent revisions."""
    repo = await _load_repo(db, owner=owner, name=name)
    if not _viewer_can_see(viewer, repo):
        # Private + unauthorized → 404 (don't leak existence).
        raise NotFoundError(f"repository not found: {owner}/{name}")
    revisions = await recent_revisions(owner, name, limit=10)
    return RepoDetail(
        id=repo.id,
        name=repo.name,
        kind=repo.kind,
        visibility=repo.visibility,
        description=repo.description,
        size_bytes=repo.size_bytes,
        owner=repo.owner.username if repo.owner else owner,
        clone_url=clone_url(repo.owner.username, repo.name) if repo.owner else "",
        created_at=repo.created_at.isoformat(),
        default_branch=repo.default_branch,
        recent_revisions=[
            {
                "commit_sha": rev.commit_sha,
                "message": rev.message,
                "author": rev.author,
                "committed_at": rev.committed_at.isoformat(),
            }
            for rev in revisions
        ],
    )


@router.patch("/{owner}/{name}", response_model=RepoSummary)
async def patch_repo(
    owner: str,
    name: str,
    body: PatchRepoRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSummary:
    """Update visibility / description (owner or admin)."""
    repo = await _load_repo(db, owner=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may modify this repo")
    if body.visibility is not None:
        repo.visibility = body.visibility.value
    if body.description is not None:
        repo.description = body.description
    await db.commit()
    await db.refresh(repo)
    return _summary(repo)


@router.delete("/{owner}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repo_route(
    owner: str,
    name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Hard-delete a repo (owner or admin)."""
    validate_slug(owner)
    validate_slug(name)
    target_user = (
        await db.execute(select(User).where(User.username == owner))
    ).scalar_one_or_none()
    if target_user is None:
        raise NotFoundError(f"user {owner!r} not found")
    repo = await _load_repo(db, owner=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may delete this repo")
    await delete_repo(db, owner=target_user, name=name, kind=RepoKind(repo.kind))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
