"""UI router for the site-wide Posts feature (feed, composer, detail).

The home page mix also reads from `outo_models.posts` for its trending
posts + top repos + running-spaces tiles, but the home route itself
stays in `ui.py` so the route-registration order remains stable.

Routes (all UI / HTML, all CSRF-protected on POST, login-required for
every write):

    GET    /posts                          — feed (latest 50, 900px wide)
    GET    /posts/new                      — long-form editor (login required)
    POST   /posts/new                      — create (short via feed modal,
                                             long via editor)
    GET    /posts/preview                  — empty long-form editor
    POST   /posts/preview                  — render body as sanitized markdown
    GET    /posts/{post_id}                — detail + reactions + comments
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
    comments_by_post,
    create_post,
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


def _post_redirect_after_action(*, post_id: int, next_token: str) -> Response:
    """Pick the redirect target for a feed-vs-detail reaction.

    `next_token == "feed"` → return to `/posts` so the chip count
    refreshes in context. Any other value (including empty) keeps
    the original permalink redirect so existing detail-page callers
    — and the existing tests — keep working.
    """
    if next_token.strip().lower() == "feed":
        return RedirectResponse(url="/posts", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url=f"/posts/{int(post_id)}", status_code=status.HTTP_303_SEE_OTHER)


def _comment_redirect_after_action(*, post_id: int, next_token: str) -> Response:
    """Pick the redirect target for a feed-vs-detail comment.

    `next_token == "feed"` → return to `/posts?comments=<id>` so the
    comments modal re-opens with the freshly-posted comment visible.
    Any other value keeps the original permalink redirect.
    """
    if next_token.strip().lower() == "feed":
        return RedirectResponse(
            url=f"/posts?comments={int(post_id)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(url=f"/posts/{int(post_id)}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/posts", response_class=HTMLResponse)
async def posts_feed_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the global posts feed (newest 50, mixed short + long).

    The feed surfaces the *applied* reactions per post as chips and
    lazy-loads comments + the new-post composer inside native
    `<details>` modals. Comments are eager-loaded in one query via
    `comments_by_post` so the per-post modal can render without an N+1
    round trip; the modal body is server-rendered when opened so a
    disabled-script CSP stays intact.
    """
    posts = list(await list_posts(db, limit=50))
    post_ids = [p.id for p in posts]
    reaction_totals = await aggregate_reactions(db, post_ids=post_ids)
    viewer_reactions = await viewer_reaction_set(db, viewer=user, post_ids=post_ids)
    comments_map = await comments_by_post(db, post_ids=post_ids)

    open_comments_raw = request.query_params.get("comments")
    open_comments_id: int | None = None
    if open_comments_raw and open_comments_raw.isdigit():
        candidate = int(open_comments_raw)
        if candidate in post_ids:
            open_comments_id = candidate

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
                "comments": comments_map.get(post.id, []),
                "open_modal": open_comments_id is not None and open_comments_id == post.id,
            }
        )

    return _form_page(
        request,
        "posts/feed.html",
        user=user,
        active_nav="posts",
        context={
            "posts_rendered": rendered,
            "reaction_palette": REACTION_PALETTE,
            "open_post_modal": request.query_params.get("post_modal") == "new",
            "post_modal_error": request.query_params.get("post_modal_error") or None,
            "post_modal_form_body": request.query_params.get("post_modal_body") or "",
            "post_modal_form_repo": request.query_params.get("post_modal_repo") or "",
        },
    )


@router.get("/posts/new", response_class=HTMLResponse)
async def posts_new_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the long-form markdown editor page (login required).

    The editor is dedicated to long posts (`kind=long`). The feed's
    short-post modal POSTs straight to `/posts/new` with `kind=short`,
    so this GET always serves the long editor.
    """
    if user is None:
        return _login_redirect_with_next("/posts/new")
    return _form_page(
        request,
        "posts/new.html",
        user=user,
        active_nav="posts",
        context={
            "form_kind": POST_KIND_LONG,
            "form_title": "",
            "form_body": "",
            "form_repo_link": "",
            "reaction_palette": REACTION_PALETTE,
            "error": None,
            "preview_html": None,
        },
    )


@router.get("/posts/preview", response_class=HTMLResponse)
async def posts_preview_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the empty long-form editor (login required)."""
    if user is None:
        return _login_redirect_with_next("/posts/preview")
    return _form_page(
        request,
        "posts/new.html",
        user=user,
        active_nav="posts",
        context={
            "form_kind": POST_KIND_LONG,
            "form_title": "",
            "form_body": "",
            "form_repo_link": "",
            "reaction_palette": REACTION_PALETTE,
            "error": None,
            "preview_html": None,
        },
    )


@router.post("/posts/preview", response_class=HTMLResponse)
async def posts_preview_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    title: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Render the editor with the body re-rendered as sanitized markdown.

    The publish form remains on the page so the user can Publish from
    the preview state without a second round trip; the publish click
    POSTs `/posts/new` with `kind=long`. Empty bodies yield an empty
    preview string so the page does not show a misleading "Empty
    post body." note.
    """
    if user is None:
        return _login_redirect_with_next("/posts/preview")
    verify_csrf(request, form_token=csrf)
    preview_html = render_long_body(body) if (body or "").strip() else ""
    return _form_page(
        request,
        "posts/new.html",
        user=user,
        active_nav="posts",
        context={
            "form_kind": POST_KIND_LONG,
            "form_title": title,
            "form_body": body,
            "form_repo_link": "",
            "reaction_palette": REACTION_PALETTE,
            "error": None,
            "preview_html": preview_html,
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
    """Validate + create a post; re-render the composer on error.

    The two call sites are visually distinct:

    * **Long editor** (`GET /posts/new` page) submits with
      `kind=long`. On error we re-render the editor page in place —
      the user keeps the draft + sees the error banner on the page
      they came from.
    * **Short modal** (the feed's `<details>` new-post widget) submits
      with `kind=short`. On error we re-render the feed page with
      `?post_modal=new` so the new-post `<details>` stays open and the
      error banner lives inside the modal — the user keeps the feed
      context and the draft fields.

    Success always redirects to the new post's permalink — that is the
    user contract, never weaken it.
    """
    if user is None:
        return _login_redirect_with_next("/posts/new")
    verify_csrf(request, form_token=csrf)

    resolved_kind = (kind or POST_KIND_SHORT).strip().lower()
    if resolved_kind not in (POST_KIND_SHORT, POST_KIND_LONG):
        resolved_kind = POST_KIND_SHORT

    def _re_render_editor(error_message: str) -> Response:
        return _form_page(
            request,
            "posts/new.html",
            user=user,
            active_nav="posts",
            context={
                "form_kind": resolved_kind,
                "form_title": title,
                "form_body": body,
                "form_repo_link": repo_link,
                "reaction_palette": REACTION_PALETTE,
                "error": error_message,
                "preview_html": None,
            },
        )

    def _re_render_feed_modal(error_message: str) -> Response:
        from urllib.parse import quote

        params = {
            "post_modal": "new",
            "post_modal_error": error_message,
            "post_modal_body": body,
            "post_modal_repo": repo_link,
        }
        qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items() if v)
        return RedirectResponse(url=f"/posts?{qs}", status_code=status.HTTP_303_SEE_OTHER)

    def _re_render(error_message: str) -> Response:
        if resolved_kind == POST_KIND_LONG:
            return _re_render_editor(error_message)
        return _re_render_feed_modal(error_message)

    try:
        repo = await resolve_repo_link(db, raw=repo_link, viewer=user)
    except Exception as exc:
        return _re_render(str(exc))

    try:
        post = await create_post(
            db,
            author=user,
            kind=resolved_kind,
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
    next: Annotated[str, Form()] = "",
) -> Response:
    """Toggle the viewer's `emoji` reaction on `post_id` (login + CSRF).

    When invoked from the feed's picker modal (`next=feed`), the
    redirect lands back on `/posts` so the updated chip counts render
    in context. The detail-page case (no `next`) keeps the existing
    redirect-to-permalink behaviour.
    """
    if user is None:
        return _login_redirect_with_next(f"/posts/{post_id}")
    verify_csrf(request, form_token=csrf)
    post = await load_post_or_404(db, post_id=post_id)
    try:
        await toggle_reaction(db, post=post, user=user, emoji=emoji)
    except Exception:
        await db.rollback()
        return _post_redirect_after_action(post_id=post_id, next_token=next)
    await db.commit()
    return _post_redirect_after_action(post_id=post_id, next_token=next)


@router.post("/posts/{post_id}/comments")
async def posts_comments_form(
    request: Request,
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    body: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
    next: Annotated[str, Form()] = "",
) -> Response:
    """Append a comment to `post_id` (login + CSRF).

    From the feed's comments modal (`next=feed`) the redirect lands on
    `/posts?comments=<id>` so the modal re-opens with the new comment
    visible. The detail-page case (no `next`) keeps the existing
    redirect-to-permalink behaviour.
    """
    if user is None:
        return _login_redirect_with_next(f"/posts/{post_id}")
    verify_csrf(request, form_token=csrf)
    post = await load_post_or_404(db, post_id=post_id)
    try:
        await add_comment(db, post=post, author=user, body=body)
    except Exception:
        await db.rollback()
    return _comment_redirect_after_action(post_id=post_id, next_token=next)


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
