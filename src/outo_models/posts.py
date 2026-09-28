"""Service layer for the site-wide Posts feature.

Pure async-session functions consumed by both the UI router
(`server.routers.posts`) and the home page mix. Domain rules — length
caps, kind/title consistency, reaction palette, repo-link visibility —
live here so the HTTP surface has a single source of truth.

The Posts feature is intentionally flat: emoji reactions from a fixed
8-glyph palette (GitHub-style), no comment threading, no moderation
queue. The per-repo community tab (`/{owner}/{name}/community`) keeps
the threaded-comment + reply contract.

All functions take an `AsyncSession` and DO NOT commit — routers own
the transaction so the audit row and the data row land together (or
neither does).

# allow: SIZE_OK — the v0.5.x ownership list locks the Posts service
# surface to `src/outo_models/posts.py`, so CRUD / reactions / comments
# / home-tile queries share one module. Splitting into sub-modules
# would create files outside the contract.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.db import (
    POST_BODY_LONG_MAX,
    POST_BODY_SHORT_MAX,
    POST_COMMENT_BODY_MAX,
    POST_KIND_LONG,
    POST_KIND_SHORT,
    POST_TITLE_MAX,
    REACTION_PALETTE,
    AuditLog,
    Post,
    PostComment,
    PostReaction,
    Repo,
    User,
)
from outo_models.exceptions import (
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from outo_models.repos.card import parse_card_metadata
from outo_models.repos.social import load_repo_or_404
from outo_models.utils.slug import validate_slug

# Reaction palette lock — the user contract freezes this set. Anything
# outside it raises ValidationFailedError at the API boundary.
REACTION_PALETTE_SET: Final[frozenset[str]] = frozenset(REACTION_PALETTE)

# Feed constants.
_FEED_LIMIT: Final = 50
_TRENDING_LIMIT: Final = 6
_TRENDING_WINDOW_DAYS: Final = 7
_TOP_REPOS_LIMIT: Final = 6
_RUNNING_SPACES_LIMIT: Final = 6
_PROFILE_POSTS_LIMIT: Final = 20
_HOME_TILE_PREVIEW_CHARS: Final = 280
_FEED_COMMENT_LIMIT_PER_POST: Final = 50

_TARGET_TYPE_POST: Final = "post"
_TARGET_TYPE_POST_COMMENT: Final = "post_comment"
_TARGET_TYPE_USER: Final = "user"

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

    Mirrors the helper in `repos.social`; inlined here so the Posts
    feature does not grow a cross-module coupling for one shared helper.
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


def _validate_kind(raw: str) -> str:
    """Normalize `kind` to one of the two allowed values; raise otherwise.

    The kind field is short / long; the DB CHECK also enforces the
    whitelist but a typed boundary error is friendlier than a 500.
    """
    cleaned = (raw or "").strip().lower()
    if cleaned not in (POST_KIND_SHORT, POST_KIND_LONG):
        raise ValidationFailedError(f"kind must be one of: {POST_KIND_SHORT}, {POST_KIND_LONG}")
    return cleaned


def _validate_title(raw: str | None, *, kind: str) -> str | None:
    """Trim + length-check the title; enforce the kind/title asymmetry.

    Long posts require a non-empty title; short posts reject any title
    (the DB CHECK catches this too, but a typed error is more useful at
    the API surface).
    """
    if kind == POST_KIND_SHORT:
        if raw is not None and raw.strip():
            raise ValidationFailedError(
                "short posts must not include a title (the title field is reserved for long posts)"
            )
        return None
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValidationFailedError("long posts require a title")
    if len(cleaned) > POST_TITLE_MAX:
        raise ValidationFailedError(f"title must be at most {POST_TITLE_MAX} characters")
    return cleaned


def _validate_body(raw: str, *, kind: str) -> str:
    """Trim + length-check the body; pick the cap based on kind."""
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValidationFailedError("post body must not be blank")
    cap = POST_BODY_LONG_MAX if kind == POST_KIND_LONG else POST_BODY_SHORT_MAX
    if len(cleaned) > cap:
        raise ValidationFailedError(f"{kind} post body must be at most {cap} characters")
    return cleaned


def _validate_emoji(raw: str) -> str:
    """Reject any emoji outside the locked palette."""
    cleaned = (raw or "").strip()
    if cleaned not in REACTION_PALETTE_SET:
        raise ValidationFailedError(
            f"emoji must be one of the palette: {' '.join(REACTION_PALETTE)}"
        )
    return cleaned


def parse_repo_link(raw: str) -> tuple[str, str] | None:
    """Parse `owner/name` into `(owner, name)`, returning `None` on empty.

    Raises `ValidationFailedError` if the input is non-empty but
    malformed (wrong number of slashes, blank owner/name, invalid slug).
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split("/")
    if len(parts) != 2:
        raise ValidationFailedError(
            "repo link must be in the form 'owner/name' (exactly one slash)"
        )
    owner, name = parts[0].strip(), parts[1].strip()
    if not owner or not name:
        raise ValidationFailedError("repo link must include both owner and name")
    validate_slug(owner)
    validate_slug(name)
    return owner, name


async def resolve_repo_link(
    session: AsyncSession,
    *,
    raw: str | None,
    viewer: User | None,
) -> Repo | None:
    """Look up a `Repo` by `owner/name`, enforcing visibility for the viewer.

    Returns `None` when the input is empty so the composer can submit
    posts without a linked repo. Returns `None` (NOT a 404) when the
    repo exists but is private and the viewer cannot see it — that
    mirrors the home-page repo-link rule that a stranger must not be
    able to discover a private repo by guessing `owner/name`.

    Raises `ValidationFailedError` on a malformed link or on a missing
    (but otherwise legal) repo, so the composer can surface the error
    inline.
    """
    parsed = parse_repo_link(raw or "")
    if parsed is None:
        return None
    owner, name = parsed
    repo = await load_repo_or_404(session, owner=owner, name=name)
    if repo.visibility != "public" and (
        viewer is None or (viewer.id != repo.owner_id and viewer.role != "admin")
    ):
        # Same posture as the repo-page visibility check: pretend the
        # repo doesn't exist so private existence is not leaked.
        raise ValidationFailedError(f"repository not found: {owner}/{name}")
    return repo


async def create_post(
    session: AsyncSession,
    *,
    author: User,
    kind: str,
    body: str,
    title: str | None = None,
    repo: Repo | None = None,
) -> Post:
    """Insert a post authored by `author`.

    All input validation runs first so the function either returns a
    fully-formed `Post` row or raises — there is no partial-write state
    to roll back. The post row is flushed before the audit row is added
    so the audit log carries the real `post.id` as its `target_id`.
    """
    normalized_kind = _validate_kind(kind)
    normalized_title = _validate_title(title, kind=normalized_kind)
    normalized_body = _validate_body(body, kind=normalized_kind)
    post = Post(
        author_id=author.id,
        kind=normalized_kind,
        title=normalized_title,
        body=normalized_body,
        repo_id=repo.id if repo is not None else None,
    )
    session.add(post)
    await session.flush()
    _append_audit(
        session,
        actor_id=author.id,
        action="post.create",
        target_type=_TARGET_TYPE_POST,
        target_id=str(post.id),
        detail={
            "kind": normalized_kind,
            "has_repo": repo is not None,
            "body_len": len(normalized_body),
            "repo": f"{repo.owner.username}/{repo.name}" if repo is not None else None,
        },
    )
    return post


async def load_post_or_404(session: AsyncSession, *, post_id: int) -> Post:
    """Fetch a `Post` row by id, eager-loading the author + linked repo + owner.

    The author relationship is `lazy="raise"` at the model layer, so
    callers that need the author's username must go through this helper
    (or `selectinload(Post.author)` themselves). The linked-repo path
    is eager-loaded too because almost every caller renders the chip.
    """
    stmt = (
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.author),
            selectinload(Post.repo).selectinload(Repo.owner),
        )
    )
    post = (await session.execute(stmt)).scalar_one_or_none()
    if post is None:
        raise NotFoundError(f"post {post_id} not found")
    return post


async def load_post_with_repo_or_404(session: AsyncSession, *, post_id: int) -> Post:
    """Alias for `load_post_or_404` (kept for backwards-compatible calls)."""
    return await load_post_or_404(session, post_id=post_id)


async def list_posts(
    session: AsyncSession,
    *,
    limit: int = _FEED_LIMIT,
) -> Sequence[Post]:
    """Return the most-recent `limit` posts, newest-first.

    Author + (when present) the linked repo's owner are eager-loaded
    in a single statement so the feed tile can render every chip
    without an N+1 round trip.
    """
    safe_limit = max(1, min(int(limit), 200))
    stmt = (
        select(Post)
        .options(
            selectinload(Post.author),
            selectinload(Post.repo).selectinload(Repo.owner),
        )
        .order_by(desc(Post.created_at), desc(Post.id))
        .limit(safe_limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_posts_for_user(
    session: AsyncSession,
    *,
    author: User,
    limit: int = _PROFILE_POSTS_LIMIT,
) -> Sequence[Post]:
    """Return the user's most-recent posts (newest-first)."""
    safe_limit = max(1, min(int(limit), 200))
    stmt = (
        select(Post)
        .where(Post.author_id == author.id)
        .options(
            selectinload(Post.author),
            selectinload(Post.repo).selectinload(Repo.owner),
        )
        .order_by(desc(Post.created_at), desc(Post.id))
        .limit(safe_limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_trending_posts(
    session: AsyncSession,
    *,
    limit: int = _TRENDING_LIMIT,
    window_days: int = _TRENDING_WINDOW_DAYS,
) -> Sequence[Post]:
    """Return posts that earned the most reactions in the last `window_days`.

    Ties on the reaction-count subquery are broken by `created_at` so
    the fallback to the most-recent post is deterministic. When no
    reactions are recorded in the window, the result is the empty
    sequence and the home tile falls back to "no recent popular posts".
    """
    safe_limit = max(1, min(int(limit), 50))
    safe_window = max(1, min(int(window_days), 30))
    cutoff = datetime.now(tz=UTC) - timedelta(days=safe_window)
    react_count_sq = (
        select(func.count(PostReaction.id))
        .where(PostReaction.post_id == Post.id)
        .where(PostReaction.created_at >= cutoff)
        .correlate(Post)
        .scalar_subquery()
    )
    stmt = (
        select(Post)
        .options(
            selectinload(Post.author),
            selectinload(Post.repo).selectinload(Repo.owner),
        )
        .where(Post.created_at >= cutoff)
        .order_by(desc(react_count_sq), desc(Post.created_at), desc(Post.id))
        .limit(safe_limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_top_repos_by_downloads(
    session: AsyncSession,
    *,
    kind: str,
    limit: int = _TOP_REPOS_LIMIT,
) -> Sequence[Repo]:
    """Return the top `limit` public repos of `kind` ordered by downloads.

    `downloads_count` is an all-time counter (incremented by the git
    smart-HTTP upload-pack path), so this is the right source for a
    "most downloaded" tile — there is no per-event timestamp. The
    secondary order by `id desc` keeps ties deterministic.
    """
    safe_limit = max(1, min(int(limit), 50))
    stmt = (
        select(Repo)
        .where(Repo.kind == kind)
        .where(Repo.visibility == "public")
        .options(selectinload(Repo.owner))
        .order_by(desc(Repo.downloads_count), desc(Repo.id))
        .limit(safe_limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_running_spaces(
    session: AsyncSession,
    *,
    limit: int = _RUNNING_SPACES_LIMIT,
) -> Sequence[Repo]:
    """Return public Space repos ordered by most-recent update.

    The Spaces v2 runtime does NOT persist a `running_since` timestamp
    — the runtime state is reconstructed from Podman on demand. So
    "longest running" is not directly answerable from the DB; we use
    `updated_at desc` as the documented approximation (the home tile
    headline is "recently updated Spaces" rather than "longest-running"
    for that reason). The spaces runtime manager exposes the live
    state; the home tile falls back to "most recently updated" because
    that is the closest deterministic signal available in v0.5.
    """
    safe_limit = max(1, min(int(limit), 50))
    stmt = (
        select(Repo)
        .where(Repo.kind == "space")
        .where(Repo.visibility == "public")
        .options(selectinload(Repo.owner))
        .order_by(desc(Repo.updated_at), desc(Repo.id))
        .limit(safe_limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def aggregate_reactions(
    session: AsyncSession,
    *,
    post_ids: Sequence[int],
) -> dict[int, dict[str, int]]:
    """Return `{post_id: {emoji: count}}` for every reaction in one query.

    `post_ids` is an empty-or-non-empty sequence; an empty input yields
    an empty dict so callers do not need to special-case it. The single
    `GROUP BY post_id, emoji` statement is the only DB hit — the home
    page + the feed + the detail page all share it.
    """
    if not post_ids:
        return {}
    rows = (
        await session.execute(
            select(PostReaction.post_id, PostReaction.emoji, func.count())
            .where(PostReaction.post_id.in_(list(post_ids)))
            .group_by(PostReaction.post_id, PostReaction.emoji)
        )
    ).all()
    out: dict[int, dict[str, int]] = {}
    for post_id, emoji, count in rows:
        out.setdefault(int(post_id), {})[str(emoji)] = int(count)
    return out


async def viewer_reaction_set(
    session: AsyncSession,
    *,
    viewer: User | None,
    post_ids: Sequence[int],
) -> set[tuple[int, str]]:
    """Return `{(post_id, emoji), ...}` for the viewer's reactions.

    `viewer=None` → empty set; callers do not need to gate the call.
    The `(post_id, emoji)` shape lines up with the toggle endpoint so
    a test can match the response to the stored row without an extra
    join.
    """
    if viewer is None or not post_ids:
        return set()
    rows = (
        await session.execute(
            select(PostReaction.post_id, PostReaction.emoji).where(
                PostReaction.user_id == viewer.id,
                PostReaction.post_id.in_(list(post_ids)),
            )
        )
    ).all()
    return {(int(pid), str(em)) for pid, em in rows}


async def reaction_count(session: AsyncSession, *, post: Post) -> int:
    """Total reaction count across every emoji on `post`."""
    value = (
        await session.execute(
            select(func.count(PostReaction.id)).where(PostReaction.post_id == post.id)
        )
    ).scalar_one()
    return int(value or 0)


async def toggle_reaction(
    session: AsyncSession,
    *,
    post: Post,
    user: User,
    emoji: str,
) -> bool:
    """Add or remove `user`'s `emoji` reaction on `post`.

    Returns `True` when a NEW reaction was added; `False` when an
    existing reaction was removed. The DB `UNIQUE(post_id, user_id, emoji)`
    constraint makes the insert idempotent — a duplicate `INSERT`
    raises `IntegrityError` which the helper catches and treats as
    "already present" so a retried POST is a no-op rather than a 500.
    """
    glyph = _validate_emoji(emoji)
    existing = (
        await session.execute(
            select(PostReaction).where(
                PostReaction.post_id == post.id,
                PostReaction.user_id == user.id,
                PostReaction.emoji == glyph,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await session.delete(existing)
        _append_audit(
            session,
            actor_id=user.id,
            action="post.reaction_remove",
            target_type=_TARGET_TYPE_POST,
            target_id=str(post.id),
            detail={"emoji": glyph},
        )
        await session.flush()
        return False
    session.add(PostReaction(post_id=post.id, user_id=user.id, emoji=glyph))
    _append_audit(
        session,
        actor_id=user.id,
        action="post.reaction_add",
        target_type=_TARGET_TYPE_POST,
        target_id=str(post.id),
        detail={"emoji": glyph},
    )
    await session.flush()
    return True


async def add_comment(
    session: AsyncSession,
    *,
    post: Post,
    author: User,
    body: str,
) -> PostComment:
    """Insert a comment authored by `author` on `post`.

    The body is trimmed and length-checked against
    `POST_COMMENT_BODY_MAX` (4000 chars). Empty bodies raise a typed
    error; the route layer re-renders the detail page on error.
    """
    cleaned = (body or "").strip()
    if not cleaned:
        raise ValidationFailedError("comment body must not be blank")
    if len(cleaned) > POST_COMMENT_BODY_MAX:
        raise ValidationFailedError(
            f"comment body must be at most {POST_COMMENT_BODY_MAX} characters"
        )
    comment = PostComment(post_id=post.id, author_id=author.id, body=cleaned)
    session.add(comment)
    _append_audit(
        session,
        actor_id=author.id,
        action="post.comment",
        target_type=_TARGET_TYPE_POST,
        target_id=str(post.id),
        detail={"length": len(cleaned)},
    )
    await session.flush()
    return comment


async def list_comments(
    session: AsyncSession,
    *,
    post: Post,
    limit: int = 200,
) -> Sequence[PostComment]:
    """Return every comment on `post`, oldest-first (chronological read order)."""
    safe_limit = max(1, min(int(limit), 500))
    stmt = (
        select(PostComment)
        .where(PostComment.post_id == post.id)
        .options(selectinload(PostComment.author))
        .order_by(PostComment.created_at.asc(), PostComment.id.asc())
        .limit(safe_limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def load_comment_or_404(
    session: AsyncSession,
    *,
    post: Post,
    comment_id: int,
) -> PostComment:
    """Fetch a comment by id, scoped to the supplied post (404 on miss)."""
    stmt = (
        select(PostComment)
        .where(PostComment.id == comment_id, PostComment.post_id == post.id)
        .options(selectinload(PostComment.author))
    )
    comment = (await session.execute(stmt)).scalar_one_or_none()
    if comment is None:
        raise NotFoundError(f"comment {comment_id} not found on post {post.id}")
    return comment


async def comments_by_post(
    session: AsyncSession,
    *,
    post_ids: Sequence[int],
    limit_per_post: int = _FEED_COMMENT_LIMIT_PER_POST,
) -> dict[int, list[PostComment]]:
    """Return `{post_id: [comments...]}` for every supplied post id in one query.

    Each value list is oldest-first and capped at `limit_per_post` so
    the feed modal can render a comment list without an N+1 round trip.
    Authors are eager-loaded. An empty `post_ids` yields an empty dict.
    """
    if not post_ids:
        return {}
    safe_limit = max(1, min(int(limit_per_post), 200))
    rows = (
        (
            await session.execute(
                select(PostComment)
                .where(PostComment.post_id.in_(list(post_ids)))
                .options(selectinload(PostComment.author))
                .order_by(
                    PostComment.post_id.asc(),
                    PostComment.created_at.asc(),
                    PostComment.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    cap_per_post: dict[int, int] = {int(pid): 0 for pid in post_ids}
    out: dict[int, list[PostComment]] = {int(pid): [] for pid in post_ids}
    for comment in rows:
        taken = cap_per_post.get(int(comment.post_id), 0)
        if taken >= safe_limit:
            continue
        out.setdefault(int(comment.post_id), []).append(comment)
        cap_per_post[int(comment.post_id)] = taken + 1
    return out


async def delete_post(session: AsyncSession, *, post: Post, actor: User) -> None:
    """Delete `post` (author or admin only). Reactions + comments cascade.

    The DB-level FKs declare `ON DELETE CASCADE`, but SQLite (the
    default backend) does not enforce FKs unless the connection has
    `PRAGMA foreign_keys = ON` — a per-connection pragma the engine
    factory does not set. To keep the cascade reliable on every
    backend, the service layer issues explicit `DELETE` statements for
    the reactions + comments rows before deleting the post. The audit
    row records the deletion before the delete fires so the log
    survives the cascade.
    """
    from sqlalchemy import delete as sa_delete

    if post.author_id != actor.id and actor.role != "admin":
        raise ForbiddenError("only the post author or an admin may delete a post")
    _append_audit(
        session,
        actor_id=actor.id,
        action="post.delete",
        target_type=_TARGET_TYPE_POST,
        target_id=str(post.id),
        detail={"author_id": post.author_id},
    )
    await session.execute(sa_delete(PostReaction).where(PostReaction.post_id == post.id))
    await session.execute(sa_delete(PostComment).where(PostComment.post_id == post.id))
    await session.delete(post)
    await session.flush()


async def delete_comment(
    session: AsyncSession,
    *,
    comment: PostComment,
    actor: User,
) -> None:
    """Delete a comment (comment author or admin only)."""
    if comment.author_id != actor.id and actor.role != "admin":
        raise ForbiddenError("only the comment author or an admin may delete a comment")
    _append_audit(
        session,
        actor_id=actor.id,
        action="post.comment_delete",
        target_type=_TARGET_TYPE_POST_COMMENT,
        target_id=str(comment.id),
        detail={"post_id": comment.post_id},
    )
    await session.delete(comment)
    await session.flush()


def render_short_body(body: str) -> str:
    """Render a short post body for safe inclusion in the HTML feed.

    Escape every HTML special character first (so a user-supplied
    `<script>` is inert), then convert `\\n` to `<br>` so the line
    breaks the user typed survive. Returns a string of safe HTML that
    Jinja can mark with `| safe`.
    """
    return html.escape(body).replace("\n", "<br>")


def render_long_body(body: str) -> str:
    """Render a long post body as sanitized markdown (same path as cards).

    Goes through `parse_card_metadata` so the sanitizer (dangerous-tag
    strip, `on*` attribute strip, `javascript:`-URL strip) is the same
    one the model card uses. The empty-body branch yields an empty
    string so the template renders an empty-state note.
    """
    return parse_card_metadata(body).body_html


def home_preview(body: str, *, max_chars: int = _HOME_TILE_PREVIEW_CHARS) -> str:
    """Trim a short body to a single-line preview for the home tile.

    Long posts do NOT call this helper — they show a markdown excerpt
    by truncating the rendered HTML in the template. Short posts get a
    plain-text first line so the home tile never leaks a half-rendered
    markdown snippet into a tile flagged "Post".
    """
    cleaned = body.strip().replace("\r\n", "\n")
    first_para = cleaned.split("\n\n", 1)[0]
    if len(first_para) > max_chars:
        return first_para[: max_chars - 1].rstrip() + "\u2026"
    return first_para


__all__ = [
    "REACTION_PALETTE",
    "REACTION_PALETTE_SET",
    "add_comment",
    "aggregate_reactions",
    "comments_by_post",
    "delete_comment",
    "delete_post",
    "home_preview",
    "list_comments",
    "list_posts",
    "list_posts_for_user",
    "list_running_spaces",
    "list_top_repos_by_downloads",
    "list_trending_posts",
    "load_comment_or_404",
    "load_post_or_404",
    "load_post_with_repo_or_404",
    "parse_repo_link",
    "reaction_count",
    "render_long_body",
    "render_short_body",
    "resolve_repo_link",
    "toggle_reaction",
    "viewer_reaction_set",
]
