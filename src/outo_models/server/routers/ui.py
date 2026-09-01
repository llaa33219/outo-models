"""HTML pages served by Jinja2.

CSRF protection is delegated to `_ui_helpers` (double-submit cookie);
this module only renders pages and wires form POSTs to the same domain
calls the JSON API uses.

# allow: SIZE_OK — the WP-13 contract bundles 5 GET pages + 2 POST
# handlers into a single UI router; splitting would split the template
# imports across files for no real readability win.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
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
from outo_models.exceptions import UnauthorizedError, ValidationFailedError
from outo_models.repos.reflog import recent_revisions
from outo_models.server.deps import get_current_user_optional, get_db
from outo_models.server.routers._ui_helpers import (
    CSRF_COOKIE,
    ensure_csrf,
    issue_csrf_cookie,
    verify_csrf,
)
from outo_models.utils.git_url import clone_url
from outo_models.utils.slug import validate_slug

router = APIRouter(tags=["ui"], include_in_schema=False)


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


async def _require_admin_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Auth + admin gate for the dashboard page (returns 403 HTML otherwise)."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    if user is None:
        raise UnauthorizedError("Authentication required")
    if user.role != "admin":
        raise StarletteHTTPException(status_code=403, detail="Admin required")
    return user


@router.get("/", response_class=HTMLResponse)
async def repos_list_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the public repos catalog."""
    response = templates.TemplateResponse(
        request,
        "repos/list.html",
        {
            "repos": (
                await db.execute(
                    select(Repo)
                    .where(Repo.visibility == "public")
                    .options(selectinload(Repo.owner))
                    .order_by(Repo.id)
                )
            )
            .scalars()
            .all(),
            "clone_url": clone_url,
        },
    )
    ensure_csrf(request, response)
    return response


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request) -> Response:
    """Render the signup form (issues a fresh CSRF cookie)."""
    response = templates.TemplateResponse(request, "auth/signup.html", {})
    issue_csrf_cookie(response=response, settings=get_settings())
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """Render the login form (issues a fresh CSRF cookie)."""
    response = templates.TemplateResponse(request, "auth/login.html", {})
    issue_csrf_cookie(response=response, settings=get_settings())
    return response


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
    response = templates.TemplateResponse(
        request,
        "repos/view.html",
        {
            "repo": repo,
            "owner": owner,
            "clone_url": clone_url(owner, name),
            "revisions": revisions,
        },
    )
    ensure_csrf(request, response)
    return response


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
    response = templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "pending": pending,
            "csrf_cookie_name": CSRF_COOKIE,
        },
    )
    ensure_csrf(request, response)
    return response


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


__all__ = ["router", "templates"]
