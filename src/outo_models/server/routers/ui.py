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
from outo_models.auth.permissions import Scope
from outo_models.auth.sessions import SESSION_COOKIE_NAME
from outo_models.config import Settings, get_settings
from outo_models.db import Repo, User
from outo_models.exceptions import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
)
from outo_models.repos.card import read_card
from outo_models.repos.create import create_repo
from outo_models.repos.files import list_files
from outo_models.repos.models import RepoKind, Visibility
from outo_models.repos.social import (
    add_comment,
    follow_user,
    follower_count,
    is_following,
    is_liked,
    like_count,
    like_repo,
    list_comments,
    load_repo_or_404,
    load_user_or_404,
    unfollow_user,
    unlike_repo,
)
from outo_models.server.deps import get_current_user_optional, get_db
from outo_models.server.routers._ui_helpers import (
    CSRF_COOKIE,
    ensure_csrf,
    form_csrf_token,
    set_csrf_cookie,
    verify_csrf,
)
from outo_models.server.routers.auth import (
    delete_personal_access_token as delete_pat,
)
from outo_models.server.routers.auth import (
    list_user_personal_access_tokens as list_user_pats,
)
from outo_models.server.routers.auth import (
    mint_personal_access_token as mint_pat,
)
from outo_models.server.routers.auth import (
    parse_scopes,
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
# Repo-page helpers (header + sidebar + per-tab panel).
#
# The HF-style repo page is a single Jinja template with three sub-views
# (`tab` ∈ {"card", "files", "community"}). Tabs are SEPARATE URLS so the
# active state lives in the path, not in a query string or a hash — a
# viewer's "Files" view is shareable as `/{owner}/{name}/files`. The
# helper below is shared by all three GET routes; the form POST routes
# also reuse `_safe_redirect_target` to pick a referrer-aware redirect.
# ---------------------------------------------------------------------------


def _kind_tab_label(repo_kind: str) -> str:
    """Card-tab label per `Repo.kind`; keeps the rest of the chrome kind-neutral."""
    return {
        "model": "Model card",
        "dataset": "Dataset card",
        "space": "Space card",
    }[repo_kind]


def _kind_sidebar_label(repo_kind: str) -> str:
    """Sidebar info-tile label per `Repo.kind`."""
    return {"model": "Model info", "dataset": "Dataset info", "space": "Space info"}[repo_kind]


def _safe_redirect_target(request: Request, *, owner: str, name: str, default_tab: str) -> str:
    """Resolve the URL to redirect a POST back to.

    Honors the `Referer` header when it points at the same repo (or the
    owner's profile for `/{owner}/follow`), otherwise falls back to a
    deterministic URL so an external referrer cannot turn the mutation
    into an open-redirect (CSRF tokens are not a substitute for an
    explicit same-origin check on the redirect target itself).

    `default_tab` is the tab path segment to append when no referrer
    is present (`""` for the card tab, `"/files"`, `"/community"`).
    `name=""` switches the prefix check to `/{owner}` so the follow
    route accepts a referrer on any page under that owner.
    """
    default = f"/{owner}/{name}{default_tab}" if name else f"/{owner}{default_tab}"
    referer = request.headers.get("referer")
    if not referer:
        return default
    # Accept only same-origin, path-prefixed URLs.
    try:
        if "://" not in referer:
            return default
        scheme_end = referer.index("://") + 3
        path_start = referer.find("/", scheme_end)
        origin = referer[:path_start]
        path = referer[path_start:] if path_start != -1 else ""
        host = request.url.hostname or ""
        request_origin = f"{request.url.scheme}://{host}"
        if origin != request_origin:
            return default
    except ValueError:
        return default
    prefix = f"/{owner}/{name}" if name else f"/{owner}"
    if not path.startswith(prefix):
        return default
    if name and path == f"/{owner}/{name}":
        return f"/{owner}/{name}"
    # Drop any query string from the referrer path.
    clean = path.split("?", 1)[0]
    return clean or default


async def _render_repo_page(
    request: Request,
    db: AsyncSession,
    viewer: User | None,
    *,
    owner: str,
    name: str,
    tab: str,
    files_path: str = "",
) -> Response:
    """Render the HF-style repo page for `<owner>/<name>` at `tab`.

    `tab` is one of `"card"`, `"files"`, `"community"`. The helper
    loads the repo + tab-specific data once and lets the Jinja template
    pick the right panel. 404s on missing repos, private repos the
    viewer cannot see, and invalid slugs (same contract as the
    previous single-route handler).

    The response is rendered through `_form_page` so the CSRF cookie is
    minted on the first GET, matching the convention every other
    form-bearing page in the UI uses (the header's like/follow/comment
    forms all need a token).
    """
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

    # --- Per-tab data -------------------------------------------------
    # `card_body_html` + `card_metadata` may be `None` when the repo has
    # no README on the default branch. The template renders an empty
    # state tile instead of an exception.
    card_metadata = None
    card_empty = True
    if tab == "card":
        try:
            card_metadata = await read_card(owner, name, default_branch=repo.default_branch)
        except Exception:
            card_metadata = None
        card_empty = card_metadata is None

    files_entries: list[dict[str, object]] = []
    files_empty = True
    files_dir = files_path
    if tab == "files":
        try:
            entries = await list_files(
                owner, name, path=files_dir, default_branch=repo.default_branch
            )
            files_entries = [
                {
                    "name": entry.name,
                    "path": entry.path,
                    "kind": entry.kind,
                    "size_bytes": entry.size_bytes,
                }
                for entry in entries
            ]
            files_empty = False
        except NotFoundError:
            files_entries = []
            files_empty = True

    comments: list[dict[str, object]] = []
    if tab == "community":
        comment_rows = await list_comments(db, repo=repo, limit=200)
        comments = [
            {
                "id": row.id,
                "author": row.author.username,
                "body": row.body,
                "created_at": row.created_at,
            }
            for row in comment_rows
        ]

    # --- Like / follow state for the header ---------------------------
    like_total = await like_count(db, repo=repo)
    viewer_liked = await is_liked(db, user=viewer, repo=repo) if viewer is not None else False
    follow_total = await follower_count(db, followee=repo.owner)
    viewer_following_owner = False
    if viewer is not None and viewer.id != repo.owner_id:
        viewer_following_owner = await is_following(db, follower=viewer, followee=repo.owner)

    # --- Sidebar data -------------------------------------------------
    sidebar_info_rows: list[tuple[str, str]] = []
    if card_metadata is not None:
        if card_metadata.task:
            sidebar_info_rows.append(("Task", card_metadata.task))
        if card_metadata.license:
            sidebar_info_rows.append(("License", card_metadata.license))
        if card_metadata.base_model:
            sidebar_info_rows.append(("Base model", card_metadata.base_model))
        if card_metadata.datasets:
            sidebar_info_rows.append(("Datasets", ", ".join(card_metadata.datasets)))
        if card_metadata.tags:
            sidebar_info_rows.append(("Tags", ", ".join(card_metadata.tags)))
        if card_metadata.language:
            sidebar_info_rows.append(("Language", ", ".join(card_metadata.language)))

    context = {
        "repo": repo,
        "owner": owner,
        "name": name,
        "clone_url": clone_url(owner, name),
        "tab": tab,
        "tab_card_label": _kind_tab_label(repo.kind),
        "tab_card_active": tab == "card",
        "tab_files_active": tab == "files",
        "tab_community_active": tab == "community",
        "tab_sidebar_label": _kind_sidebar_label(repo.kind),
        # Card tab:
        "card_metadata": card_metadata,
        "card_empty": card_empty,
        # Files tab:
        "files_entries": files_entries,
        "files_empty": files_empty,
        "files_path": files_dir,
        "files_parent": ("/".join(files_dir.rsplit("/", 1)[:-1]) if "/" in files_dir else ""),
        # Community tab:
        "comments": comments,
        # Social state for the header:
        "like_count_value": like_total,
        "viewer_liked": viewer_liked,
        "follower_count_value": follow_total,
        "viewer_following_owner": viewer_following_owner,
        "viewer_is_owner": viewer is not None and viewer.id == repo.owner_id,
        "owner_display_name": repo.owner.display_name,
        # Sidebar:
        "sidebar_info_rows": sidebar_info_rows,
        "has_readme": card_metadata is not None,
    }

    return _form_page(
        request,
        "repos/view.html",
        user=viewer,
        active_nav=_kind_to_nav(repo.kind),
        context=context,
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


# ---------------------------------------------------------------------------
# Access tokens settings page (`/settings/tokens`).
#
# Registered BEFORE the `/{username}` catch-all so the literal path wins.
# Reuses the same token helpers as `/api/auth/tokens` — the API surface
# (`mint_pat`, `list_user_pats`, `delete_pat`, `parse_scopes`) is unchanged.
# ---------------------------------------------------------------------------


def _scope_choices() -> list[tuple[str, str]]:
    """Labeled scope pairs grouped for the create-card checkboxes.

    Order is deliberate — least-powerful first — so the default un-ticked
    rendering nudges users toward the minimum scope their workflow needs.
    """
    return [
        (Scope.READ.value, "Read"),
        (Scope.WRITE.value, "Write (push repos)"),
        (Scope.ADMIN.value, "Admin"),
    ]


def _ttl_choices() -> list[tuple[int, str]]:
    return [(30, "30 days"), (90, "90 days"), (365, "365 days")]


async def _collect_token_rows(db: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Read the current user's PATs into a template-friendly list of dicts."""
    rows = await list_user_pats(db, user=user)
    return [
        {
            "id": row.id,
            "name": row.name,
            "prefix": row.prefix,
            "scopes": parse_scopes(row.scopes),
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]


@router.get("/settings/tokens", response_class=HTMLResponse)
async def settings_tokens_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the Access Tokens settings page (login required)."""
    if user is None:
        return RedirectResponse(
            url="/login?next=/settings/tokens",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    rows = await _collect_token_rows(db, user)
    return _form_page(
        request,
        "users/tokens.html",
        user=user,
        active_nav=None,
        context={
            "tokens": rows,
            "scope_choices": _scope_choices(),
            "ttl_choices": _ttl_choices(),
            "form_name": "",
            "form_scopes": [Scope.READ.value, Scope.WRITE.value],
            "form_ttl_days": 90,
            "raw_token": None,
            "clone_url_sample": clone_url(user.username, "<repo>"),
            "error": None,
        },
    )


@router.post("/settings/tokens")
async def settings_tokens_create(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(_require_login_user)],
    name: Annotated[str, Form()],
    scopes: Annotated[list[str] | None, Form()] = None,
    ttl_days: Annotated[int, Form()] = 90,
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Create a PAT and re-render the page with the raw token shown once."""
    verify_csrf(request, form_token=csrf)

    submitted_scopes = list(scopes or [])
    name_clean = (name or "").strip()
    if not name_clean:
        return _form_page(
            request,
            "users/tokens.html",
            user=user,
            active_nav=None,
            context={
                "tokens": await _collect_token_rows(db, user),
                "scope_choices": _scope_choices(),
                "ttl_choices": _ttl_choices(),
                "form_name": name_clean,
                "form_scopes": submitted_scopes,
                "form_ttl_days": ttl_days,
                "raw_token": None,
                "clone_url_sample": clone_url(user.username, "<repo>"),
                "error": "Token name is required.",
            },
        )
    if ttl_days not in (30, 90, 365):
        ttl_days = 90

    valid_scope_values = {choice[0] for choice in _scope_choices()}
    chosen_scopes = [s for s in submitted_scopes if s in valid_scope_values]
    if not chosen_scopes:
        chosen_scopes = [Scope.READ.value, Scope.WRITE.value]

    _, raw_token = await mint_pat(
        db,
        user=user,
        name=name_clean,
        scopes=chosen_scopes,
        ttl_days=ttl_days,
        settings=settings,
    )
    return _form_page(
        request,
        "users/tokens.html",
        user=user,
        active_nav=None,
        context={
            "tokens": await _collect_token_rows(db, user),
            "scope_choices": _scope_choices(),
            "ttl_choices": _ttl_choices(),
            "form_name": "",
            "form_scopes": [Scope.READ.value, Scope.WRITE.value],
            "form_ttl_days": 90,
            "raw_token": raw_token,
            "clone_url_sample": clone_url(user.username, "<repo>"),
            "error": None,
        },
    )


@router.post("/settings/tokens/{token_id}/delete")
async def settings_tokens_delete(
    request: Request,
    token_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_login_user)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Revoke a PAT owned by the current user (or any PAT if admin)."""
    verify_csrf(request, form_token=csrf)
    await delete_pat(db, token_id=token_id, actor=user)
    return RedirectResponse(
        url="/settings/tokens",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
async def repo_card_page(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the repo page with the *card* tab selected."""
    return await _render_repo_page(request, db, viewer, owner=owner, name=name, tab="card")


@router.get("/{owner}/{name}/files", response_class=HTMLResponse)
async def repo_files_page(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    path: Annotated[str, Query()] = "",
) -> Response:
    """Render the repo page with the *files* tab selected.

    The active path is taken from the `?path=` query string so the URL
    stays canonical (`/{owner}/{name}/files?path=src`). Directory rows
    link into deeper paths via the same query parameter.
    """
    return await _render_repo_page(
        request, db, viewer, owner=owner, name=name, tab="files", files_path=path
    )


@router.get("/{owner}/{name}/community", response_class=HTMLResponse)
async def repo_community_page(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the repo page with the *community* tab selected."""
    return await _render_repo_page(request, db, viewer, owner=owner, name=name, tab="community")


@router.post("/{owner}/{name}/like")
async def repo_like_form(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Toggle the viewer's like on `/{owner}/{name}` (login + CSRF required).

    Anonymous callers are 303'd to `/login?next=...` so they can come
    back to the same tab after authenticating. The like toggle itself
    is idempotent (POST likes an unliked repo, unlikes a liked repo),
    mirroring the JSON API at `/api/repos/{owner}/{name}/like` while
    reusing the same domain helper.
    """
    if viewer is None:
        next_path = _safe_redirect_target(request, owner=owner, name=name, default_tab="")
        return RedirectResponse(
            url=f"/login?next={next_path}", status_code=status.HTTP_303_SEE_OTHER
        )
    verify_csrf(request, form_token=csrf)
    repo = await load_repo_or_404(db, owner=owner, name=name)
    already_liked = await is_liked(db, user=viewer, repo=repo)
    if already_liked:
        await unlike_repo(db, user=viewer, repo=repo)
    else:
        await like_repo(db, user=viewer, repo=repo)
    await db.commit()
    target = _safe_redirect_target(request, owner=owner, name=name, default_tab="")
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{owner}/{name}/comments")
async def repo_comments_form(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    body: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Post a comment on `/{owner}/{name}` (login + CSRF required).

    Successful POSTs redirect back to the community tab so the new
    comment appears in the rendered list; the redirect respects the
    `Referer` header when present (so a comment posted from inside
    the `/files` tab still ends up on `/community` if the user came
    from there — but the form button lives on `/community`, so the
    default target is the community tab).
    """
    if viewer is None:
        return RedirectResponse(
            url=f"/login?next=/{owner}/{name}/community",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    verify_csrf(request, form_token=csrf)
    repo = await load_repo_or_404(db, owner=owner, name=name)
    clean = body.strip()
    if clean:
        await add_comment(db, author=viewer, repo=repo, body=clean)
        await db.commit()
    target = _safe_redirect_target(request, owner=owner, name=name, default_tab="/community")
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{owner}/follow")
async def user_follow_form(
    request: Request,
    owner: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Toggle the viewer's follow on the repo owner (login + CSRF required).

    The POST target is `/{owner}/follow` because the follow button on
    the repo page targets the OWNER of the repo, not the repo itself —
    this matches the JSON API at `/api/users/{username}/follow`. The
    form button is hidden for the owner themselves so self-follow is
    not reachable through the UI; a hand-crafted POST still gets
    rejected with a 403 from the domain layer (`follow_user` raises
    `ForbiddenError`).
    """
    if viewer is None:
        return RedirectResponse(
            url="/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    verify_csrf(request, form_token=csrf)
    try:
        validate_slug(owner)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    followee = await load_user_or_404(db, username=owner)
    # Self-follow is impossible here because the form button is hidden
    # for the owner — but a manual POST must still fail cleanly.
    if viewer.id == followee.id:
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "message": "users cannot follow themselves"},
        )
    if await is_following(db, follower=viewer, followee=followee):
        await unfollow_user(db, follower=viewer, followee=followee)
    else:
        await follow_user(db, follower=viewer, followee=followee)
    await db.commit()
    target = _safe_redirect_target(request, owner=owner, name="", default_tab="")
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


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
