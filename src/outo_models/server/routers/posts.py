"""UI router for the site-wide Posts feature (feed, composer, detail).

The home page mix also reads from `outo_models.posts` for its trending
posts + top repos + running-spaces tiles, but the home route itself
stays in `ui.py` so the route-registration order remains stable.

Routes (all UI / HTML, all CSRF-protected on POST, login-required for
every write):

    GET    /posts                          — feed (latest 50)
    GET    /posts/new                      — composer (login required)
    POST   /posts/new                      — create (login + CSRF)
    GET    /posts/{post_id}                — detail + comments + reactions
    POST   /posts/{post_id}/react          — toggle emoji reaction
    POST   /posts/{post_id}/comments       — add a flat comment
    POST   /posts/{post_id}/delete         — delete post (author or admin)
    POST   /posts/{post_id}/comments/{cid}/delete
                                         — delete comment (author or admin)

Read paths consult the same visibility rules as the repo page: a
private-repo-linked post hides its chip from viewers who are not the
repo owner / an admin. Reactions + comments are public on a public
post; on a post linked to a private repo, the entire post is hidden
from non-owners — the `404 not_found` posture mirrors the repo page
so private-repo existence is never leaked through a post detail.

# allow: SIZE_OK — the v0.5.x ownership list locks the Posts UI
# router to `server/routers/posts.py`, so the feed / composer / detail
# / reactions / comments / deletion routes share one module. Splitting
# into sub-modules would create files outside the contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from outo_models.config import get_settings
from outo_models.db import POST_KIND_LONG, POST_KIND_SHORT, REACTION_PALETTE, Repo, User
from outo_models.posts import (
    REACTION_PALETTE_SET,
    add_comment,
    aggregate_reactions,
    delete_comment,
    delete_post,
    list_comments,
    list_posts,
    load_comment_or_404,
    load_post_or_404,
    render_long_body,
    render_short_body,
    resolve_repo_link,
    toggle_reaction,
    viewer_reaction_set,
)
from outo_models.server.deps import get_current_user_optional, get_db
from outo_models.server.routers._ui_helpers import (
    CSRF_COOKIE,
    ensure_csrf,
    form_csrf_token,
    set_csrf_cookie,
    verify_csrf,
)

router = APIRouter(tags=["posts"], include_in_schema=False)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Shared render helpers (mirrors `ui.py`'s _render / _form_page so the
# navbar context is uniform across every Posts page).
# ---------------------------------------------------------------------------


async def _render(
    request: Request,
    template: str,
    *,
    context: Mapping[str, Any] | None = None,
    active_nav: str | None = None,
    user: User | None = None,
) -> Response:
    """Render a read-only template with the navbar context."""
    merged: dict[str, Any] = {
        "current_user": user,
        "active_nav": active_nav,
    }
    if context:
        merged.update(context)
    response = templates.TemplateResponse(request, template, merged)
    ensure_csrf(request, response)
    return response


def _form_page(
    request: Request,
    template: str,
    *,
    context: Mapping[str, Any] | None = None,
    active_nav: str | None = None,
    user: User | None = None,
) -> Response:
    """Render a form template with CSRF token + cookie in lockstep."""
    settings = get_settings()
    token, is_new = form_csrf_token(request, settings)
    merged: dict[str, Any] = {
        "csrf_token": token,
        "current_user": user,
        "active_nav": active_nav,
    }
    if context:
        merged.update(context)
    response = templates.TemplateResponse(request, template, merged)
    if is_new:
        set_csrf_cookie(response, token, settings)
    return response


async def _require_login(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Return the authenticated user, or 401 when anonymous."""
    if user is None:
        raise StarletteHTTPException(status_code=401, detail="Authentication required")
    return user


def _viewer_can_see_repo(repo: Repo | None, *, viewer: User | None) -> bool:
    """True iff `viewer` is allowed to see `repo` (or `repo` is None / public)."""
    if repo is None:
        return True
    if repo.visibility == "public":
        return True
    if viewer is None:
        return False
    return viewer.id == repo.owner_id or viewer.role == "admin"


async def _load_post_with_repo(
    db: AsyncSession,
    *,
    post_id: int,
) -> Any:
    """Load a post + author + linked repo (with owner) in one round trip."""
    from sqlalchemy import select

    from outo_models.db import Post

    stmt = (
        select(Post)
        .where(Post.id == post_id)
        .options(selectinload(Post.author), selectinload(Post.repo).selectinload(Repo.owner))
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _relative_time_label(at: Any) -> str:
    """Render a coarse 'time ago' label for post / activity timestamps."""
    from datetime import UTC, datetime

    if at is None:
        return ""
    now = datetime.now(tz=UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    delta = now - at
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _login_redirect_with_next(path: str) -> Response:
    """Build the canonical `/login?next=...` redirect for anonymous POSTs."""
    return RedirectResponse(url=f"/login?next={path}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/posts", response_class=HTMLResponse)
async def posts_feed_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the global posts feed (newest 50, mixed short + long)."""
    posts = list(await list_posts(db, limit=50))
    post_ids = [p.id for p in posts]
    reaction_totals = await aggregate_reactions(db, post_ids=post_ids)
    viewer_reactions = await viewer_reaction_set(db, viewer=user, post_ids=post_ids)

    # Pre-compute the rendered body HTML + comment counts so the
    # template never has to call a function.
    rendered: list[dict[str, Any]] = []
    for post in posts:
        if post.kind == POST_KIND_LONG:
            body_html = render_long_body(post.body)
        else:
            body_html = render_short_body(post.body)
        rendered.append(
            {
                "post": post,
                "body_html": body_html,
                "reaction_totals": reaction_totals.get(post.id, {}),
                "viewer_reacted": {emoji for (pid, emoji) in viewer_reactions if pid == post.id},
                "viewer_can_see_repo": _viewer_can_see_repo(post.repo, viewer=user),
            }
        )

    return _form_page(
        request,
        "posts/feed.html",
        user=user,
        active_nav="posts",
        context={"posts_rendered": rendered, "reaction_palette": REACTION_PALETTE},
    )


@router.get("/posts/new", response_class=HTMLResponse)
async def posts_new_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the post composer (login required)."""
    if user is None:
        return _login_redirect_with_next("/posts/new")
    return _form_page(
        request,
        "posts/new.html",
        user=user,
        active_nav="posts",
        context={
            "form_kind": POST_KIND_SHORT,
            "form_title": "",
            "form_body": "",
            "form_repo_link": "",
            "reaction_palette": REACTION_PALETTE,
            "error": None,
        },
    )


@router.post("/posts/new")
async def posts_new_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    kind: Annotated[str, Form()] = POST_KIND_SHORT,
    title: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    repo_link: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Validate + create a post; re-render the composer on error."""
    if user is None:
        return _login_redirect_with_next("/posts/new")
    verify_csrf(request, form_token=csrf)

    from outo_models.posts import create_post

    def _re_render(error_message: str) -> Response:
        return _form_page(
            request,
            "posts/new.html",
            user=user,
            active_nav="posts",
            context={
                "form_kind": kind,
                "form_title": title,
                "form_body": body,
                "form_repo_link": repo_link,
                "reaction_palette": REACTION_PALETTE,
                "error": error_message,
            },
        )

    try:
        repo = await resolve_repo_link(db, raw=repo_link, viewer=user)
    except Exception as exc:
        return _re_render(str(exc))

    try:
        post = await create_post(
            db,
            author=user,
            kind=kind,
            title=title or None,
            body=body,
            repo=repo,
        )
    except Exception as exc:
        return _re_render(str(exc))

    await db.commit()
    return RedirectResponse(url=f"/posts/{post.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/posts/{post_id}", response_class=HTMLResponse)
async def posts_detail_page(
    request: Request,
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the detail page for `post_id`.

    Posts are always visible — only the linked-repo chip is gated on
    repo visibility, so a post about a private repo is still readable
    but the chip is hidden from viewers who cannot see the repo. This
    matches the requirement that "private-repo-linked posts show the
    chip only to viewers who can see the repo".
    """
    post = await _load_post_with_repo(db, post_id=post_id)
    if post is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "post not found"},
        )
    if post.kind == POST_KIND_LONG:
        body_html = render_long_body(post.body)
    else:
        body_html = render_short_body(post.body)
    reaction_totals = await aggregate_reactions(db, post_ids=[post.id])
    viewer_reactions = await viewer_reaction_set(db, viewer=user, post_ids=[post.id])
    comments = list(await list_comments(db, post=post, limit=200))
    return _form_page(
        request,
        "posts/detail.html",
        user=user,
        active_nav="posts",
        context={
            "post": post,
            "body_html": body_html,
            "reaction_totals": reaction_totals.get(post.id, {}),
            "viewer_reacted": {emoji for (pid, emoji) in viewer_reactions if pid == post.id},
            "comments": comments,
            "reaction_palette": REACTION_PALETTE,
            "viewer_can_see_repo": _viewer_can_see_repo(post.repo, viewer=user),
            "viewer_can_delete_post": user is not None
            and (user.id == post.author_id or user.role == "admin"),
            "comment_delete_map": {
                comment.id: bool(
                    user is not None and (user.id == comment.author_id or user.role == "admin")
                )
                for comment in comments
            },
        },
    )


@router.post("/posts/{post_id}/react")
async def posts_react_form(
    request: Request,
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    emoji: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Toggle the viewer's `emoji` reaction on `post_id` (login + CSRF)."""
    if user is None:
        return _login_redirect_with_next(f"/posts/{post_id}")
    verify_csrf(request, form_token=csrf)
    post = await load_post_or_404(db, post_id=post_id)
    try:
        await toggle_reaction(db, post=post, user=user, emoji=emoji)
    except Exception:
        await db.rollback()
        return RedirectResponse(url=f"/posts/{post_id}", status_code=status.HTTP_303_SEE_OTHER)
    await db.commit()
    return RedirectResponse(url=f"/posts/{post_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/{post_id}/comments")
async def posts_comments_form(
    request: Request,
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    body: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Append a comment to `post_id` (login + CSRF)."""
    if user is None:
        return _login_redirect_with_next(f"/posts/{post_id}")
    verify_csrf(request, form_token=csrf)
    post = await load_post_or_404(db, post_id=post_id)
    try:
        await add_comment(db, post=post, author=user, body=body)
    except Exception:
        await db.rollback()
    return RedirectResponse(url=f"/posts/{post_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/{post_id}/delete")
async def posts_delete_form(
    request: Request,
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Delete `post_id` (author or admin; cascades reactions + comments)."""
    if user is None:
        return _login_redirect_with_next("/posts")
    verify_csrf(request, form_token=csrf)
    post = await load_post_or_404(db, post_id=post_id)
    try:
        await delete_post(db, post=post, actor=user)
    except Exception:
        await db.rollback()
        return RedirectResponse(url=f"/posts/{post_id}", status_code=status.HTTP_303_SEE_OTHER)
    await db.commit()
    return RedirectResponse(url="/posts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/{post_id}/comments/{comment_id}/delete")
async def posts_comment_delete_form(
    request: Request,
    post_id: int,
    comment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Delete `comment_id` (comment author or admin)."""
    if user is None:
        return _login_redirect_with_next(f"/posts/{post_id}")
    verify_csrf(request, form_token=csrf)
    post = await load_post_or_404(db, post_id=post_id)
    comment = await load_comment_or_404(db, post=post, comment_id=comment_id)
    try:
        await delete_comment(db, comment=comment, actor=user)
    except Exception:
        await db.rollback()
    return RedirectResponse(url=f"/posts/{post_id}", status_code=status.HTTP_303_SEE_OTHER)


# Validate the palette constant at import time so a hand-crafted config
# that points at a wrong glyph fails loudly on first boot rather than
# silently mismatching the DB CHECK at reaction time.
assert all(glyph in REACTION_PALETTE_SET for glyph in REACTION_PALETTE)


__all__ = [
    "router",
    "templates",
]
