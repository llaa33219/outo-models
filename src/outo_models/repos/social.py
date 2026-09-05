"""Social-graph service layer: likes, follows, comments.

All functions take an `AsyncSession` and DO NOT commit — routers own the
transaction so a single `db.commit()` covers the row write plus the
audit-log entry the helper appends. Idempotent operations (`like_repo`,
`unlike_repo`, `follow_user`, `unfollow_user`) tolerate repeat calls so
the HTTP endpoints can answer 201/200 with the same payload.

# allow: SIZE_OK — the v0.3.0 ownership list locks the social surface
# to `src/outo_models/repos/social.py`, so likes / follows / comments
# share one module. Splitting into sub-modules would create files
# outside the contract.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.db import AuditLog, Repo, RepoComment, RepoLike, User, UserFollow
from outo_models.exceptions import ForbiddenError, NotFoundError, ValidationFailedError

_COMMENT_BODY_MAX = 4000

_TARGET_TYPE_REPO = "repo"
_TARGET_TYPE_USER = "user"

_audit = AuditLog


def _append_audit(
    session: AsyncSession,
    *,
    actor_id: int,
    action: str,
    target_type: str,
    target_id: str,
    detail: dict[str, object] | None = None,
) -> None:
    """Append an audit-log row sharing the same session as the caller.

    Routers commit in a single transaction so the audit row and the
    social-row write land together; a partial failure rolls both back.
    """
    session.add(
        _audit(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=json.dumps(detail) if detail else None,
        )
    )


async def is_liked(session: AsyncSession, *, user: User, repo: Repo) -> bool:
    """Return `True` iff `user` has liked `repo`."""
    row = (
        await session.execute(
            select(RepoLike.id).where(RepoLike.user_id == user.id, RepoLike.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    return row is not None


async def like_count(session: AsyncSession, *, repo: Repo) -> int:
    """Return the number of likes on `repo`."""
    value = (
        await session.execute(select(func.count(RepoLike.id)).where(RepoLike.repo_id == repo.id))
    ).scalar_one()
    return int(value or 0)


async def like_repo(session: AsyncSession, *, user: User, repo: Repo) -> bool:
    """Insert a like if missing; return `True` only on the first insert.

    Idempotent: a second call returns `False` without raising so routers
    can answer 200 on the repeat. The audit row is emitted exactly once,
    on the insert path. Callers must pass a repo with the `owner`
    relationship eager-loaded (see `load_repo_or_404`).
    """
    existing = (
        await session.execute(
            select(RepoLike.id).where(RepoLike.user_id == user.id, RepoLike.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(RepoLike(user_id=user.id, repo_id=repo.id))
    _append_audit(
        session,
        actor_id=user.id,
        action="repo.like",
        target_type=_TARGET_TYPE_REPO,
        target_id=str(repo.id),
        detail={"repo": f"{repo.owner.username}/{repo.name}"},
    )
    await session.flush()
    return True


async def unlike_repo(session: AsyncSession, *, user: User, repo: Repo) -> bool:
    """Delete the like if present; return `True` only when a row was removed."""
    existing = (
        await session.execute(
            select(RepoLike).where(RepoLike.user_id == user.id, RepoLike.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    _append_audit(
        session,
        actor_id=user.id,
        action="repo.unlike",
        target_type=_TARGET_TYPE_REPO,
        target_id=str(repo.id),
        detail={"repo": f"{repo.owner.username}/{repo.name}"},
    )
    await session.flush()
    return True


async def is_following(session: AsyncSession, *, follower: User, followee: User) -> bool:
    """Return `True` iff `follower` follows `followee`."""
    row = (
        await session.execute(
            select(UserFollow.id).where(
                UserFollow.follower_id == follower.id,
                UserFollow.followee_id == followee.id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def follower_count(session: AsyncSession, *, followee: User) -> int:
    """Return the number of users following `followee`."""
    value = (
        await session.execute(
            select(func.count(UserFollow.id)).where(UserFollow.followee_id == followee.id)
        )
    ).scalar_one()
    return int(value or 0)


async def follow_user(session: AsyncSession, *, follower: User, followee: User) -> bool:
    """Insert a follow edge if missing; return `True` only on the first insert.

    Raises `ForbiddenError` on self-follow so the API returns 403 instead
    of letting the DB CHECK constraint surface as a 500.
    """
    if follower.id == followee.id:
        raise ForbiddenError("users cannot follow themselves")
    existing = (
        await session.execute(
            select(UserFollow.id).where(
                UserFollow.follower_id == follower.id,
                UserFollow.followee_id == followee.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(UserFollow(follower_id=follower.id, followee_id=followee.id))
    _append_audit(
        session,
        actor_id=follower.id,
        action="user.follow",
        target_type=_TARGET_TYPE_USER,
        target_id=str(followee.id),
        detail={"follower": follower.username, "followee": followee.username},
    )
    await session.flush()
    return True


async def unfollow_user(session: AsyncSession, *, follower: User, followee: User) -> bool:
    """Delete the follow edge if present; return `True` only on removal."""
    existing = (
        await session.execute(
            select(UserFollow).where(
                UserFollow.follower_id == follower.id,
                UserFollow.followee_id == followee.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    _append_audit(
        session,
        actor_id=follower.id,
        action="user.unfollow",
        target_type=_TARGET_TYPE_USER,
        target_id=str(followee.id),
        detail={"follower": follower.username, "followee": followee.username},
    )
    await session.flush()
    return True


async def add_comment(
    session: AsyncSession,
    *,
    author: User,
    repo: Repo,
    body: str,
) -> RepoComment:
    """Insert a comment authored by `author` on `repo`.

    Raises `ValidationFailedError` on a blank body or one longer than
    4000 chars; the API surface mirrors the same cap so the constraint
    is enforced exactly once.
    """
    stripped = body.strip()
    if not stripped:
        raise ValidationFailedError("comment body must not be blank")
    if len(body) > _COMMENT_BODY_MAX:
        raise ValidationFailedError(f"comment body must be at most {_COMMENT_BODY_MAX} characters")
    comment = RepoComment(repo_id=repo.id, author_id=author.id, body=body)
    session.add(comment)
    _append_audit(
        session,
        actor_id=author.id,
        action="repo.comment",
        target_type=_TARGET_TYPE_REPO,
        target_id=str(repo.id),
        detail={
            "repo": f"{repo.owner.username}/{repo.name}",
            "length": len(body),
        },
    )
    await session.flush()
    return comment


async def list_comments(
    session: AsyncSession,
    *,
    repo: Repo,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[RepoComment]:
    """Return comments for `repo`, newest-first, eager-loading authors.

    `limit` is clamped to a sane upper bound so a buggy caller cannot
    page the entire table; `offset` is non-negative for the same reason.
    The author relationship is eager-loaded because the API response
    carries the author's username.
    """
    safe_limit = max(1, min(int(limit), 200))
    safe_offset = max(0, int(offset))
    stmt = (
        select(RepoComment)
        .where(RepoComment.repo_id == repo.id)
        .options(selectinload(RepoComment.author))
        .order_by(RepoComment.created_at.desc(), RepoComment.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return rows


async def load_repo_or_404(session: AsyncSession, *, owner: str, name: str) -> Repo:
    """Fetch a `Repo` by `(owner, name)` with the owner relationship loaded.

    Centralises the lookup + 404 so callers do not scatter `select(Repo)`;
    the eager-loaded owner matches the API response contract.
    """
    repo = (
        await session.execute(
            select(Repo)
            .where(Repo.name == name)
            .options(selectinload(Repo.owner))
            .join(Repo.owner)
            .where(User.username == owner)
        )
    ).scalar_one_or_none()
    if repo is None:
        raise NotFoundError(f"repository not found: {owner}/{name}")
    return repo


async def load_user_or_404(session: AsyncSession, *, username: str) -> User:
    """Fetch a `User` by username; raise `NotFoundError` on miss."""
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"user {username!r} not found")
    return user


__all__ = [
    "add_comment",
    "follow_user",
    "follower_count",
    "is_following",
    "is_liked",
    "like_count",
    "like_repo",
    "list_comments",
    "load_repo_or_404",
    "load_user_or_404",
    "unfollow_user",
    "unlike_repo",
]
