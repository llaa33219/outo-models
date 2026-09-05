"""HTML pages served by Jinja2.

CSRF protection is delegated to `_ui_helpers` (double-submit cookie);
this module only renders pages and wires form POSTs to the same domain
calls the JSON API uses.

The route registration order is intentional: the static-prefix routes
(`/models`, `/datasets`, `/spaces`, `/new`, `/login`, `/signup`,
`/admin`) and the one-segment parameterised route (`/{username}`)
are registered BEFORE the two-segment `/{owner}/{name}` catch-all so
Starlette resolves the more specific paths first. A one-segment match
does not conflict with a two-segment URL by construction, but the
ordering documents the intent.

Every page goes through `_render` (read-only) or `_form_page` (forms)
so the navbar context (current user, active section) is uniform.

# allow: SIZE_OK — the WP-13 contract bundles 8 GET pages + 3 POST
# handlers into a single UI router; splitting would split the template
# imports across files for no real readability win.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from outo_models.auth import (
    SessionManager,
    can_login,
    cookie_kwargs,
    register_user,
    verify_password,
)
from outo_models.auth.sessions import SESSION_COOKIE_NAME
from outo_models.config import Settings, get_settings
from outo_models.db import Repo, User
from outo_models.exceptions import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
)
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility
from outo_models.repos.reflog import recent_revisions
from outo_models.server.deps import get_current_user_optional, get_db
from outo_models.server.routers._ui_helpers import (
    CSRF_COOKIE,
    ensure_csrf,
    form_csrf_token,
    set_csrf_cookie,
    verify_csrf,
)
from outo_models.spaces.registry import create_space
from outo_models.utils.git_url import clone_url
from outo_models.utils.slug import validate_slug

router = APIRouter(tags=["ui"], include_in_schema=False)


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Shared render helpers
# ---------------------------------------------------------------------------


async def _render(
    request: Request,
    template: str,
    *,
    context: Mapping[str, Any] | None = None,
    active_nav: str | None = None,
    user: User | None = None,
) -> Response:
    """Render a template with the standard navbar context (read-only pages).

    Resolves `current_user` from the session cookie so every page knows
    whether to show login/signup or the profile chip. `active_nav` is
    the kind key used by `base.html` to highlight the active tab; pass
    `None` for non-listing pages. CSRF cookie is minted best-effort so a
    later form POST always has one to send back.
    """
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
    """Render a form template with CSRF token + cookie in lockstep.

    The `csrf_token` in context MUST equal the `_csrf` cookie on the
    same response. Starlette ≥1.x renders TemplateResponse eagerly at
    construction, so the token is minted BEFORE the response exists
    and the cookie attached afterwards. The navbar's `current_user`
    is included so the chip / login state is consistent on form pages
    too.
    """
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


async def _require_admin_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Auth + admin gate for the dashboard page (403 HTML otherwise)."""
    if user is None:
        raise UnauthorizedError("Authentication required")
    if user.role != "admin":
        raise StarletteHTTPException(status_code=403, detail="Admin required")
    return user


def _kind_label(repo_kind: RepoKind) -> str:
    """Human-friendly plural label (e.g. for empty-state messages)."""
    return {
        RepoKind.MODEL: "models",
        RepoKind.DATASET: "datasets",
        RepoKind.SPACE: "spaces",
    }[repo_kind]


def _kind_to_nav(repo_kind: str) -> str | None:
    """Translate `Repo.kind` → the nav-bar `active_nav` key, or `None`."""
    return {"model": "models", "dataset": "datasets", "space": "spaces"}.get(repo_kind)


async def _require_login_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Gate the /new POST: redirect anonymous callers to /login."""
    if user is None:
        raise UnauthorizedError("Authentication required")
    return user


async def _render_kind_list(
    request: Request,
    db: AsyncSession,
    user: User | None,
    repo_kind: RepoKind,
) -> Response:
    """Shared backend for /models, /datasets, /spaces."""
    repos = (
        (
            await db.execute(
                select(Repo)
                .where(Repo.kind == repo_kind.value)
                .where(Repo.visibility == "public")
                .options(selectinload(Repo.owner))
                .order_by(Repo.id)
            )
        )
        .scalars()
        .all()
    )
    headings = {
        RepoKind.MODEL: "Models",
        RepoKind.DATASET: "Datasets",
        RepoKind.SPACE: "Spaces",
    }
    active_nav = _kind_to_nav(repo_kind.value)
    return await _render(
        request,
        "repos/by_kind.html",
        user=user,
        active_nav=active_nav,
        context={
            "repos": repos,
            "repo_kind": repo_kind.value,
            "kind_label": _kind_label(repo_kind),
            "heading": headings[repo_kind],
        },
    )


# ---------------------------------------------------------------------------
# Routes (ordered: specifics before the catch-all `/{owner}/{name}`).
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def repos_list_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the public repos catalog (the home page)."""
    repos = (
        (
            await db.execute(
                select(Repo)
                .where(Repo.visibility == "public")
                .options(selectinload(Repo.owner))
                .order_by(Repo.id)
            )
        )
        .scalars()
        .all()
    )
    return await _render(
        request,
        "repos/list.html",
        user=user,
        active_nav=None,
        context={"repos": repos, "clone_url": clone_url},
    )


@router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """List public Models (HF-style catalog)."""
    return await _render_kind_list(request, db, user, RepoKind.MODEL)


@router.get("/datasets", response_class=HTMLResponse)
async def datasets_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """List public Datasets."""
    return await _render_kind_list(request, db, user, RepoKind.DATASET)


@router.get("/spaces", response_class=HTMLResponse)
async def spaces_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """List public Spaces."""
    return await _render_kind_list(request, db, user, RepoKind.SPACE)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request) -> Response:
    """Render the signup form (issues a fresh CSRF cookie on first visit)."""
    return _form_page(request, "auth/signup.html")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """Render the login form (issues a fresh CSRF cookie on first visit)."""
    return _form_page(request, "auth/login.html")


@router.get("/new", response_class=HTMLResponse)
async def new_repo_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    kind: Annotated[str | None, Query()] = None,
) -> Response:
    """Render the repo-creation form (login-gated)."""
    if user is None:
        return RedirectResponse(url="/login?next=/new", status_code=status.HTTP_303_SEE_OTHER)
    initial_kind = (kind or "model").lower()
    if initial_kind not in {k.value for k in RepoKind}:
        initial_kind = "model"
    return _form_page(
        request,
        "repos/new.html",
        user=user,
        active_nav=None,
        context={
            "form_kind": initial_kind,
            "form_name": "",
            "form_visibility": "private",
            "form_description": "",
            "error": None,
        },
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_page(
    request: Request,
    user: Annotated[User, Depends(_require_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the admin dashboard."""
    pending = (
        (await db.execute(select(User).where(User.status == "pending").order_by(User.created_at)))
        .scalars()
        .all()
    )
    return _form_page(
        request,
        "admin/dashboard.html",
        user=user,
        active_nav=None,
        context={"pending": pending, "csrf_cookie_name": CSRF_COOKIE},
    )


@router.get("/logout", response_class=HTMLResponse)
async def logout_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the logout confirmation tile (login required).

    Anonymous callers are redirected to `/login` — there is no point in
    confirming a sign-out for a client that isn't signed in. The form
    below the heading POSTs to the same path with the CSRF token.
    """
    if user is None:
        return RedirectResponse(url="/login?next=/logout", status_code=status.HTTP_303_SEE_OTHER)
    return _form_page(request, "auth/logout.html", user=user, active_nav=None)


@router.post("/logout")
async def logout_form(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Clear the session cookie and redirect home.

    The double-submit CSRF cookie is verified before any cookie is
    mutated so a third-party site cannot force a sign-out. The session
    cookie is cleared by overwriting it with an empty value and
    `max_age=0` — that is the Starlette `Response.delete_cookie` shape.
    Idempotent: posting with no session is a no-op.
    """
    verify_csrf(request, form_token=csrf)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.env == "production",
    )
    return response


@router.get("/{username}", response_class=HTMLResponse)
async def user_profile_page(
    request: Request,
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the Hugging Face-style profile page.

    Shows the user's avatar (initial), name, joined date, and a tabbed
    list of their Models / Datasets / Spaces. 404s when the username
    has no matching row.
    """
    try:
        validate_slug(username)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    profile = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    repos = (
        (
            await db.execute(
                select(Repo)
                .join(Repo.owner)
                .where(User.username == username)
                .options(selectinload(Repo.owner))
                .order_by(Repo.id)
            )
        )
        .scalars()
        .all()
    )
    is_self = user is not None and user.username == username
    viewer_is_admin = user is not None and user.role == "admin"

    grouped: dict[str, list[Repo]] = {"model": [], "dataset": [], "space": []}
    for repo in repos:
        if repo.visibility != Visibility.PUBLIC.value and not is_self and not viewer_is_admin:
            continue
        grouped.setdefault(repo.kind, []).append(repo)

    return await _render(
        request,
        "users/profile.html",
        user=user,
        active_nav=None,
        context={
            "profile": profile,
            "grouped": grouped,
            "is_self": is_self,
            "viewer_is_admin": viewer_is_admin,
        },
    )


@router.get("/{owner}/{name}", response_class=HTMLResponse)
async def repo_detail_page(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render a single repo's overview + clone command + recent commits."""
    try:
        validate_slug(owner)
        validate_slug(name)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    repo = (
        await db.execute(
            select(Repo)
            .where(Repo.name == name)
            .options(selectinload(Repo.owner))
            .join(Repo.owner)
            .where(User.username == owner)
        )
    ).scalar_one_or_none()
    if repo is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    if repo.visibility != "public" and (
        viewer is None or (viewer.id != repo.owner_id and viewer.role != "admin")
    ):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    revisions = await recent_revisions(owner, name, limit=20)
    return await _render(
        request,
        "repos/view.html",
        user=viewer,
        active_nav=_kind_to_nav(repo.kind),
        context={
            "repo": repo,
            "owner": owner,
            "clone_url": clone_url(owner, name),
            "revisions": revisions,
        },
    )


# ---------------------------------------------------------------------------
# Form POSTs (CSRF-protected).
# ---------------------------------------------------------------------------


@router.post("/signup")
async def signup_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    username: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Receive the signup form, register the user, redirect to login."""
    verify_csrf(request, form_token=csrf)
    user = await register_user(
        db,
        username=username,
        email=email,
        password=password,
        settings=settings,
    )
    await db.commit()
    target = "/login"
    if user.status == "approved":
        target = "/"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/login")
async def login_form(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Receive the login form, set the session cookie, redirect home."""
    verify_csrf(request, form_token=csrf)
    slug = validate_slug(username)
    user = (await db.execute(select(User).where(User.username == slug))).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    can_login(user)
    manager = SessionManager(settings.secret_key, max_age=7 * 24 * 3600)
    token = manager.dumps({"user_id": user.id, "nonce": secrets.token_urlsafe(16)})
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        **cookie_kwargs(secure=settings.env == "production"),
    )
    return response


@router.post("/new")
async def new_repo_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_login_user)],
    kind: Annotated[str, Form()],
    name: Annotated[str, Form()],
    visibility: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Create a new model / dataset / space; redirect to its repo page."""
    verify_csrf(request, form_token=csrf)

    def _re_render(error_message: str) -> Response:
        return _form_page(
            request,
            "repos/new.html",
            user=user,
            active_nav=None,
            context={
                "form_kind": kind,
                "form_name": name,
                "form_visibility": visibility,
                "form_description": description,
                "error": error_message,
            },
        )

    try:
        repo_kind = RepoKind(kind.lower())
    except ValueError:
        return _re_render(f"Unknown repository kind: {kind!r}.")
    try:
        visibility_enum = Visibility(visibility.lower())
    except ValueError:
        return _re_render(f"Unknown visibility: {visibility!r}.")
    try:
        clean_name = validate_slug(name)
    except ValidationFailedError as exc:
        return _re_render(str(exc))

    description_clean = description.strip() or None
    try:
        if repo_kind == RepoKind.SPACE:
            created = await create_space(
                db,
                owner=user,
                name=clean_name,
                sdk="static",
                visibility=visibility_enum,
                description=description_clean,
            )
        else:
            created = await create_repo(
                db,
                owner=user,
                name=clean_name,
                kind=repo_kind,
                visibility=visibility_enum,
                description=description_clean,
            )
    except (ConflictError, ValidationFailedError, NotFoundError) as exc:
        # Rollback any partial writes from the failed create, then refresh
        # `user` so the form re-render's `current_user.username` access doesn't
        # reach back into an expired SQLAlchemy state (sync lazy-load from a
        # sync context → MissingGreenlet).
        await db.rollback()
        await db.refresh(user)
        return _re_render(str(exc))

    await db.commit()
    repo_name = created.name if created is not None else clean_name
    return RedirectResponse(
        url=f"/{user.username}/{repo_name}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ["router", "templates"]
