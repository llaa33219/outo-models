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

import contextlib
import json
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
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
from outo_models.db import AuditLog, Repo, RepoComment, RepoLike, User
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
from outo_models.repos.quota import ensure_quota_rows
from outo_models.repos.social import (
    add_comment,
    follow_user,
    follower_count,
    is_following,
    is_liked,
    like_count,
    like_repo,
    list_comments,
    list_likes,
    load_repo_or_404,
    load_user_or_404,
    recent_activity,
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
from outo_models.server.routers.users import (
    _validate_interests as _api_validate_interests,
)
from outo_models.server.routers.users import (
    _validate_links as _api_validate_links,
)
from outo_models.spaces import (
    SpaceRuntimeManager,
    create_space,
    read_space_meta,
)
from outo_models.spaces import (
    runtime_status as runtime_status_async,
)
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


# ---------------------------------------------------------------------------
# Repo accent color palette (UI chrome — pick from a curated, BLP-friendly
# set so the catalog never gets garish). The same list is reused by the
# profile-page edit form (no JS color picker) and the repo header picker.
# The hex values come from the 디자인.md auxiliary palette (§3.2).
# ---------------------------------------------------------------------------


REPO_COLOR_PALETTE: list[dict[str, str]] = [
    {"value": "", "label": "None", "hex": ""},
    {"value": "#DBEDFF", "label": "Sky", "hex": "#DBEDFF"},
    {"value": "#D4DCE8", "label": "Mist", "hex": "#D4DCE8"},
    {"value": "#DBE3FF", "label": "Periwinkle", "hex": "#DBE3FF"},
    {"value": "#EEF5FC", "label": "Paper", "hex": "#EEF5FC"},
    {"value": "#7FBCFF", "label": "Soft blue", "hex": "#7FBCFF"},
    {"value": "#B8E1D8", "label": "Mint", "hex": "#B8E1D8"},
    {"value": "#F2F6ED", "label": "Lime", "hex": "#F2F6ED"},
    {"value": "#FCF5EE", "label": "Apricot", "hex": "#FCF5EE"},
    {"value": "#D67FFF", "label": "Lilac", "hex": "#D67FFF"},
]
_REPO_COLOR_HEX_VALUES: set[str] = {entry["hex"] for entry in REPO_COLOR_PALETTE if entry["hex"]}


def _is_valid_palette_color(color: str | None) -> bool:
    """`True` if `color` is `None` or one of the palette hexes."""
    if color is None or color == "":
        return True
    return color.lower() in {hex_val.lower() for hex_val in _REPO_COLOR_HEX_VALUES}


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_palette_color(raw: str | None) -> str | None:
    """Normalize a form-submitted color value.

    The picker is a `<select>` with the curated palette as the only
    legal values, but a hand-crafted POST could send anything; accept
    `None` / empty (cleared), the palette entries, or any `#RRGGBB` so
    the JSON PATCH endpoint at `/api/repos/{owner}/{name}` stays
    reachable. Anything else raises `ValidationFailedError`.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.lower() in {hex_val.lower() for hex_val in _REPO_COLOR_HEX_VALUES}:
        return cleaned.lower()
    if _HEX_COLOR_RE.match(cleaned):
        return cleaned.lower()
    raise ValidationFailedError("color must be empty or in the form #RRGGBB (6 hex digits)")


def _human_bytes(n: int) -> str:
    """Render a byte count as a human-readable string for the usage page.

    Uses binary (1024-based) units and one decimal of precision, capped
    at tebibytes — anything larger is shown as `XX.X TiB` so the label
    stays one line. Zero / negative inputs render as `0 B` so the
    template never has to special-case the empty-account state.
    """
    if n <= 0:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(n)
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[idx]}"


def _relative_time(at: datetime) -> str:
    """Render a coarse 'relative-ish' label for the recent-activity tile.

    The site is server-rendered with no JS clock; the timestamp itself is
    kept available to the template via `activity_at_iso` so the user can
    inspect the exact moment. The visible label is the rounded duration
    in seconds/minutes/hours/days/years — coarse on purpose.
    """
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


def _decode_json_field(raw: str | None) -> list[Any]:
    """Parse a JSON list column (interests/links) into a Python list.

    Returns an empty list on missing / malformed JSON so the template can
    iterate without an `is None` check; mirrors the JSON API's behavior
    in `routers/users.get_profile`.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


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
    *,
    q: str | None = None,
    owner_filter: str | None = None,
    sort: str | None = None,
) -> Response:
    """Shared backend for /models, /datasets, /spaces.

    `q` matches `name` OR `description` case-insensitively (server-side,
    SQL `func.lower()` LIKE so the index is irrelevant for a substring
    scan at v0.4 — Postgres installs can swap to ILIKE later). `owner`
    matches the owner's `username` exactly (slug, case-insensitive).
    `sort` ∈ {None, "recent", "downloads", "likes"}:

    - `None` / `"recent"` → most recent first by `Repo.id` desc
    - `"downloads"`     → highest `downloads_count` first
    - `"likes"`         → highest `like_count` first (subquery)

    The likes sort uses a correlated subquery over `RepoLike` so we do
    NOT loop per repo (one query batches the count). Counts are
    computed in SQL, never in Python.
    """
    from sqlalchemy import desc, func, or_

    _ALLOWED_SORT = {"recent", "downloads", "likes"}
    sort_value = sort if sort in _ALLOWED_SORT else "recent"

    stmt = (
        select(Repo)
        .where(Repo.kind == repo_kind.value)
        .where(Repo.visibility == "public")
        .options(selectinload(Repo.owner))
    )
    if q:
        like = f"%{q.lower()}%"
        lowered_name = func.lower(Repo.name)
        lowered_desc = func.lower(func.coalesce(Repo.description, ""))
        stmt = stmt.where(or_(lowered_name.like(like), lowered_desc.like(like)))
    if owner_filter:
        try:
            validate_slug(owner_filter)
        except ValidationFailedError:
            owner_filter = None
        if owner_filter:
            stmt = stmt.join(Repo.owner).where(User.username == owner_filter)

    if sort_value == "downloads":
        stmt = stmt.order_by(desc(Repo.downloads_count), desc(Repo.id))
    elif sort_value == "likes":
        like_count_sq = (
            select(func.count(RepoLike.repo_id))
            .where(RepoLike.repo_id == Repo.id)
            .correlate(Repo)
            .scalar_subquery()
        )
        stmt = stmt.order_by(desc(like_count_sq), desc(Repo.id))
    else:
        stmt = stmt.order_by(desc(Repo.id))

    repos = (await db.execute(stmt)).scalars().all()

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
            "filter_q": q or "",
            "filter_owner": owner_filter or "",
            "filter_sort": sort_value,
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

    `tab` is one of `"card"`, `"files"`, `"community"`, `"settings"`.
    The helper loads the repo + tab-specific data once and lets the
    Jinja template pick the right panel. 404s on missing repos, private
    repos the viewer cannot see, and invalid slugs (same contract as
    the previous single-route handler).

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

    threads: list[dict[str, object]] = []
    likes_users: list[dict[str, object]] = []
    likes_overflow_count = 0
    if tab == "community":
        comment_rows = await list_comments(db, repo=repo, limit=200)
        rows_by_id: dict[int, RepoComment] = {row.id: row for row in comment_rows}
        top_level = [row for row in comment_rows if row.parent_id is None]
        replies_by_root: dict[int, list[RepoComment]] = {}
        for row in comment_rows:
            if row.parent_id is None:
                continue
            # Walk the parent chain to find the top-level ancestor so
            # replies-to-replies flatten into the same chain (the
            # `len(rows_by_id)` bound defends against a pathological
            # self-referencing parent_id cycle).
            root_id = row.parent_id
            for _ in range(len(rows_by_id)):
                parent = rows_by_id.get(root_id)
                if parent is None or parent.parent_id is None:
                    break
                root_id = parent.parent_id
            replies_by_root.setdefault(root_id, []).append(row)
        for reply_list in replies_by_root.values():
            reply_list.sort(key=lambda r: (r.created_at, r.id))
        threads = [
            {
                "top": {
                    "id": top.id,
                    "author": top.author.username,
                    "body": top.body,
                    "created_at": top.created_at,
                },
                "replies": [
                    {
                        "id": reply.id,
                        "author": reply.author.username,
                        "body": reply.body,
                        "created_at": reply.created_at,
                    }
                    for reply in replies_by_root.get(top.id, [])
                ],
            }
            for top in top_level
        ]
        liker_rows = await list_likes(db, repo=repo, limit=100)
        likes_users = [
            {
                "username": user.username,
                "display_name": user.display_name,
            }
            for user in liker_rows
        ]
        like_total = await like_count(db, repo=repo)
        if like_total > len(likes_users):
            likes_overflow_count = like_total - len(likes_users)

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

    # Spaces runtime tile — present only when the repo IS a space. The
    # disabled branch surfaces an admin hint (set OUTO_SPACES_RUNTIME_ENABLED)
    # instead of Start/Stop capsules; any exception vs Podman is swallowed
    # and shown as a "failed" tile rather than 500.
    space_runtime: dict[str, Any] | None = None
    if repo.kind == "space":
        from outo_models.spaces.runtime import RuntimeState, RuntimeStatus

        settings = request.app.state.settings
        try:
            rs: RuntimeStatus = await runtime_status_async(
                repo,
                settings=settings,
                manager=SpaceRuntimeManager(settings),
            )
        except Exception as exc:
            rs = RuntimeStatus(
                state=RuntimeState.FAILED,
                message=f"Failed to contact the runtime manager: {exc}",
                url=None,
            )
        space_runtime = {
            "state": rs.state.value,
            "message": rs.message,
            "url": rs.url,
            "container_id": rs.container_id,
            "port": rs.port,
            "sdk": read_space_meta(owner, name).sdk,
            "runtime_enabled": bool(getattr(settings, "spaces_runtime_enabled", False)),
        }

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
        "tab_settings_active": tab == "settings",
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
        "threads": threads,
        "likes_users": likes_users,
        "likes_overflow_count": likes_overflow_count,
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
        # Color picker (header — owner only).
        "color_palette": REPO_COLOR_PALETTE,
        "color_error": request.query_params.get("color_error"),
        # Spaces runtime tile (None for non-space repos; populated by
        # the dispatcher above for kind="space").
        "space_runtime": space_runtime,
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


@router.get("/static/{filename}", include_in_schema=False)
async def static_asset(filename: str) -> Response:
    """Serve bundled static assets (wheel-packaged under assets/static/).

    Only `.js` files, no path traversal — the response is content so the
    BLP chrome rules do not apply to it, but the route itself is chrome.
    """
    if "/" in filename or "\\" in filename or not filename.endswith(".js"):
        raise NotFoundError(f"no such asset: {filename}")
    # Reading via importlib.resources keeps ASYNC240 happy (async route) and
    # works identically from a wheel zip or a source tree.
    import importlib.resources as resources

    asset_ref = resources.files("outo_models.assets") / "static" / filename
    if not asset_ref.is_file():
        raise NotFoundError(f"no such asset: {filename}")
    return Response(
        content=asset_ref.read_bytes(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


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
    q: Annotated[str | None, Query()] = None,
    owner: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
) -> Response:
    """List public Models (HF-style catalog)."""
    return await _render_kind_list(
        request, db, user, RepoKind.MODEL, q=q, owner_filter=owner, sort=sort
    )


@router.get("/datasets", response_class=HTMLResponse)
async def datasets_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    q: Annotated[str | None, Query()] = None,
    owner: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
) -> Response:
    """List public Datasets."""
    return await _render_kind_list(
        request, db, user, RepoKind.DATASET, q=q, owner_filter=owner, sort=sort
    )


@router.get("/spaces", response_class=HTMLResponse)
async def spaces_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    q: Annotated[str | None, Query()] = None,
    owner: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
) -> Response:
    """List public Spaces."""
    return await _render_kind_list(
        request, db, user, RepoKind.SPACE, q=q, owner_filter=owner, sort=sort
    )


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
            "form_color": "",
            "color_palette": REPO_COLOR_PALETTE,
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


@router.get("/support", response_class=HTMLResponse)
async def support_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the support contact page.

    The operator email is read from `Settings.support_email` when set;
    absent that, the page renders a generic "ask the operator" note so
    no visitor sees a stray placeholder address. The page is public so
    unauthenticated visitors can still reach the operator — but the
    navbar is identical to every other public page (login / signup for
    anonymous viewers).
    """
    settings = get_settings()
    operator_email = getattr(settings, "support_email", None) or None
    return await _render(
        request,
        "support.html",
        user=user,
        active_nav=None,
        context={"operator_email": operator_email},
    )


@router.get("/{username}", response_class=HTMLResponse)
async def user_profile_page(
    request: Request,
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the Hugging Face-style profile page.

    Shows the user's avatar (initial), display name, bio, interests,
    external links, recent activity, and a tabbed list of their
    Models / Datasets / Spaces — split into a left profile tile + a
    right repos tile per the v0.5 layout. 404s when the username has
    no matching row.
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

    interests = _decode_json_field(profile.interests)
    raw_links = _decode_json_field(profile.links)
    links: list[dict[str, str]] = [
        item for item in raw_links if isinstance(item, dict) and "label" in item and "url" in item
    ]
    activity = await recent_activity(db, user=profile, limit=10)

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
            "interests": interests,
            "links": links,
            "activity": activity,
        },
    )


@router.get("/{username}/edit", response_class=HTMLResponse)
async def profile_edit_page(
    request: Request,
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the edit-profile form (owner only)."""
    if user is None:
        return RedirectResponse(
            url=f"/login?next=/{username}/edit",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        validate_slug(username)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    if user.username != username:
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "message": "you can only edit your own profile"},
        )
    profile = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    interests = _decode_json_field(profile.interests)
    raw_links = _decode_json_field(profile.links)
    initial_links: list[dict[str, str]] = []
    for item in raw_links:
        if isinstance(item, dict) and "label" in item and "url" in item:
            label = str(item.get("label", ""))
            url = str(item.get("url", ""))
            initial_links.append({"label": label, "url": url})
    # Pad to 8 link rows so the form has a stable shape even when fewer are saved.
    while len(initial_links) < 8:
        initial_links.append({"label": "", "url": ""})

    return _form_page(
        request,
        "users/profile_edit.html",
        user=user,
        active_nav=None,
        context={
            "profile": profile,
            "form_display_name": profile.display_name or "",
            "form_bio": profile.bio or "",
            "form_interests_csv": ", ".join(interests),
            "form_links": initial_links,
            "error": None,
            "error_field": None,
        },
    )


@router.post("/{username}/edit")
async def profile_edit_form(
    request: Request,
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_login_user)],
    display_name: Annotated[str, Form()] = "",
    bio: Annotated[str, Form()] = "",
    interests_csv: Annotated[str, Form(alias="interests")] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Save the form-submitted profile fields (owner only, CSRF-protected).

    Re-renders the form on validation failure so the user fixes the bad
    field in-place; success redirects back to `/{username}`.
    """
    verify_csrf(request, form_token=csrf)
    try:
        validate_slug(username)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    if user.username != username:
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "message": "you can only edit your own profile"},
        )
    profile = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if profile is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )

    # Parse + validate the form inputs using the same rules as the JSON
    # API (single-sourced via `_api_validate_*` from `routers/users.py`).
    raw_form = dict(await request.form())
    labels: list[str] = []
    urls: list[str] = []
    for idx in range(8):
        labels.append(str(raw_form.get(f"link_label_{idx}", "") or "").strip())
        urls.append(str(raw_form.get(f"link_url_{idx}", "") or "").strip())

    cleaned_links = [
        {"label": label, "url": url}
        for label, url in zip(labels, urls, strict=False)
        if label or url
    ]

    interests_list = [raw.strip() for raw in interests_csv.split(",") if raw.strip()]

    error: str | None = None
    error_field: str | None = None
    cleaned_display_name: str | None = display_name.strip() or None
    cleaned_bio: str | None = bio.strip() or None

    if cleaned_display_name is not None and len(cleaned_display_name) > 80:
        error = "Display name must be 80 characters or fewer."
        error_field = "display_name"
    elif cleaned_bio is not None and len(cleaned_bio) > 2000:
        error = "Bio must be 2000 characters or fewer."
        error_field = "bio"
    else:
        try:
            cleaned_interests = _api_validate_interests(interests_list) if interests_list else []
        except ValidationFailedError as exc:
            error = str(exc)
            error_field = "interests"
        else:
            try:
                cleaned_links_list = _api_validate_links(cleaned_links) if cleaned_links else []
            except ValidationFailedError as exc:
                error = str(exc)
                error_field = "links"

    if error is None:
        profile.display_name = cleaned_display_name
        profile.bio = cleaned_bio
        if cleaned_links:
            profile.links = json.dumps(cleaned_links_list)
        else:
            profile.links = None
        if interests_list:
            profile.interests = json.dumps(cleaned_interests)
        else:
            profile.interests = None
        db.add(
            AuditLog(
                actor_id=user.id,
                action="user.profile_update",
                target_type="user",
                target_id=str(profile.id),
            )
        )
        await db.commit()
        return RedirectResponse(url=f"/{username}", status_code=status.HTTP_303_SEE_OTHER)

    # Re-render the form with the typed values + error inline.
    initial_links = [{"label": label, "url": url} for label, url in zip(labels, urls, strict=False)]
    while len(initial_links) < 8:
        initial_links.append({"label": "", "url": ""})

    return _form_page(
        request,
        "users/profile_edit.html",
        user=user,
        active_nav=None,
        context={
            "profile": profile,
            "form_display_name": display_name,
            "form_bio": bio,
            "form_interests_csv": interests_csv,
            "form_links": initial_links,
            "error": error,
            "error_field": error_field,
        },
    )


@router.get("/{username}/usage", response_class=HTMLResponse)
async def user_usage_page(
    request: Request,
    username: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the per-user storage usage page (self-only).

    The page is gated to the owner of the profile — anyone else gets a
    403 (the navbar and the existing profile surface both still link
    here, but a non-self visitor receives a clear "this is for you
    only" error instead of leaking the quota numbers of someone else).
    Redirects anonymous viewers to `/login?next=...` so the deep link
    survives a sign-in round-trip.
    """
    if viewer is None:
        return RedirectResponse(
            url=f"/login?next=/{username}/usage",
            status_code=status.HTTP_303_SEE_OTHER,
        )
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
    if viewer.username != profile.username:
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "message": "you can only view your own usage"},
        )
    quota_row, usage_row = await ensure_quota_rows(db, profile)
    used_bytes = int(usage_row.used_bytes)
    max_bytes = int(quota_row.max_bytes)
    free_bytes = max(0, max_bytes - used_bytes)
    used_fraction = used_bytes / max_bytes if max_bytes > 0 else 0.0
    used_pct = used_fraction * 100.0
    used_pct_rounded = round(used_pct)
    used_pct_capped = max(0, min(100, used_pct))
    if used_pct >= 100.0:
        usage_band = "over"
    elif used_pct >= 80.0:
        usage_band = "warn"
    else:
        usage_band = "ok"
    return _form_page(
        request,
        "users/usage.html",
        user=viewer,
        active_nav=None,
        context={
            "profile": profile,
            "used_bytes": used_bytes,
            "max_bytes": max_bytes,
            "free_bytes": free_bytes,
            "used_human": _human_bytes(used_bytes),
            "quota_human": _human_bytes(max_bytes),
            "free_human": _human_bytes(free_bytes),
            "used_fraction": used_fraction,
            "used_pct_rounded": used_pct_rounded,
            "used_pct_capped": used_pct_capped,
            "usage_band": usage_band,
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


@router.get("/{owner}/{name}/settings", response_class=HTMLResponse)
async def repo_settings_page(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Render the repo settings tab (owner/admin only).

    The tab nav entry is hidden for non-owner / non-admin viewers, so
    a hand-crafted GET must still fail cleanly with 403 / 404 instead
    of leaking the form. The repo lookup reuses the same visibility
    gate as the other repo GETs so a private repo a non-owner could
    not see on the card tab stays invisible here too.
    """
    if viewer is None:
        return RedirectResponse(
            url=f"/login?next=/{owner}/{name}/settings",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        validate_slug(owner)
        validate_slug(name)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    repo = await load_repo_or_404(db, owner=owner, name=name)
    if repo.visibility != "public" and (viewer.id != repo.owner_id and viewer.role != "admin"):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    if viewer.id != repo.owner_id and viewer.role != "admin":
        return JSONResponse(
            status_code=403,
            content={
                "error": "forbidden",
                "message": "only the owner or an admin may edit settings",
            },
        )
    return _form_page(
        request,
        "repos/settings.html",
        user=viewer,
        active_nav=_kind_to_nav(repo.kind),
        context={
            "repo": repo,
            "owner": owner,
            "name": name,
            "form_visibility": repo.visibility,
            "form_description": repo.description or "",
            "form_color": repo.color or "",
            "color_palette": REPO_COLOR_PALETTE,
            "error": None,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/{owner}/{name}/settings")
async def repo_settings_form(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    visibility: Annotated[str, Form()] = "public",
    description: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Save repo settings (owner/admin only, CSRF-protected).

    Writes through the domain layer (`repo.visibility / description /
    color`) so the same validation rules as the JSON PATCH endpoint
    apply — a single source of truth for what counts as a legal value.
    The color is normalized via the shared `_validate_palette_color`
    helper so a hand-crafted POST with a bogus value fails the same
    way the header picker does. Renames are intentionally not wired:
    the template renders the name as read-only with a "not supported
    yet" note because they would invalidate every clone URL pointing
    at the old name.
    """
    if viewer is None:
        return RedirectResponse(
            url=f"/login?next=/{owner}/{name}/settings",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    verify_csrf(request, form_token=csrf)
    try:
        validate_slug(owner)
        validate_slug(name)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    repo = await load_repo_or_404(db, owner=owner, name=name)
    if repo.visibility != "public" and (viewer.id != repo.owner_id and viewer.role != "admin"):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    if viewer.id != repo.owner_id and viewer.role != "admin":
        return JSONResponse(
            status_code=403,
            content={
                "error": "forbidden",
                "message": "only the owner or an admin may edit settings",
            },
        )

    try:
        normalized_visibility = Visibility(visibility.lower())
    except ValueError:
        normalized_visibility = None
    try:
        normalized_color = _validate_palette_color(color)
    except ValidationFailedError as exc:
        return _form_page(
            request,
            "repos/settings.html",
            user=viewer,
            active_nav=_kind_to_nav(repo.kind),
            context={
                "repo": repo,
                "owner": owner,
                "name": name,
                "form_visibility": repo.visibility,
                "form_description": description,
                "form_color": color,
                "color_palette": REPO_COLOR_PALETTE,
                "error": str(exc),
                "saved": False,
            },
        )

    description_clean = description.strip() or None
    if normalized_visibility is None:
        return _form_page(
            request,
            "repos/settings.html",
            user=viewer,
            active_nav=_kind_to_nav(repo.kind),
            context={
                "repo": repo,
                "owner": owner,
                "name": name,
                "form_visibility": repo.visibility,
                "form_description": description_clean or "",
                "form_color": color,
                "color_palette": REPO_COLOR_PALETTE,
                "error": f"Unknown visibility: {visibility!r}.",
                "saved": False,
            },
        )

    repo.visibility = normalized_visibility.value
    repo.description = description_clean
    repo.color = normalized_color
    db.add(
        AuditLog(
            actor_id=viewer.id,
            action="repo.settings_update",
            target_type="repo",
            target_id=str(repo.id),
        )
    )
    await db.commit()
    return RedirectResponse(
        url=f"/{owner}/{name}/settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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


@router.post("/{owner}/{name}/color")
async def repo_color_form(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    color: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Update the repo accent color from the header picker (owner only).

    Mirrors the JSON PATCH at `/api/repos/{owner}/{name}` for the
    `color` field but writes through `repo.color` directly so the form
    path does not need an extra HTTP round-trip. The same `#RRGGBB`
    validation applies (`""` / palette-only / hex).
    """
    if user is None:
        target = _safe_redirect_target(request, owner=owner, name=name, default_tab="")
        return RedirectResponse(
            url=f"/login?next={target}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    verify_csrf(request, form_token=csrf)
    try:
        validate_slug(owner)
        validate_slug(name)
    except ValidationFailedError:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "not found"},
        )
    repo = await load_repo_or_404(db, owner=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "message": "only the owner may change the color"},
        )
    try:
        new_color = _validate_palette_color(color)
    except ValidationFailedError as exc:
        target = _safe_redirect_target(request, owner=owner, name=name, default_tab="")
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{sep}color_error={exc}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    repo.color = new_color
    db.add(
        AuditLog(
            actor_id=user.id,
            action="repo.color_update",
            target_type="repo",
            target_id=str(repo.id),
        )
    )
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
    parent_id: Annotated[str | None, Form()] = None,
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    """Post a comment or reply on `/{owner}/{name}` (login + CSRF required).

    Accepts an optional `parent_id` form field — when present, the
    new row is attached as a reply to that top-level comment
    (replies-to-replies are flattened onto the same top-level chain
    by the template). Successful POSTs redirect back to the community
    tab so the new comment appears in the rendered list; the redirect
    respects the `Referer` header when present (so a comment posted
    from inside the `/files` tab still ends up on `/community` if the
    user came from there — but the form button lives on `/community`,
    so the default target is the community tab).
    """
    if viewer is None:
        return RedirectResponse(
            url=f"/login?next=/{owner}/{name}/community",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    verify_csrf(request, form_token=csrf)
    repo = await load_repo_or_404(db, owner=owner, name=name)
    clean = body.strip()
    parsed_parent_id: int | None = None
    if parent_id is not None and parent_id.strip():
        try:
            parsed_parent_id = int(parent_id)
        except ValueError:
            parsed_parent_id = None
    if clean:
        await add_comment(db, author=viewer, repo=repo, body=clean, parent_id=parsed_parent_id)
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
    color: Annotated[str, Form()] = "",
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
                "form_color": color or "",
                "color_palette": REPO_COLOR_PALETTE,
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

    try:
        normalized_color = _validate_palette_color(color)
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
                color=normalized_color,
            )
        else:
            created = await create_repo(
                db,
                owner=user,
                name=clean_name,
                kind=repo_kind,
                visibility=visibility_enum,
                description=description_clean,
                color=normalized_color,
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


# Spaces runtime UI POSTs — owner-only start/stop form targets. They
# delegate to `_run_lifecycle` in `routers/spaces.py` so the JSON API
# and the HTML form stay behaviour-aligned (audit log, Podman call,
# error semantics). Failures are swallowed so the next GET surfaces
# the `RuntimeStatus.message` instead of a 500.


async def _owner_or_403(repo: Repo, user: User | None) -> User:
    if user is None:
        raise UnauthorizedError("Authentication required")
    if user.id != repo.owner_id and user.role != "admin":
        raise StarletteHTTPException(status_code=403, detail="Space owner only")
    return user


def _space_runtime_redirect_target(request: Request, owner: str, name: str) -> str:
    return _safe_redirect_target(request, owner=owner, name=name, default_tab="")


@router.post("/{owner}/{name}/space/start")
async def space_start_form(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    if user is None:
        target = _space_runtime_redirect_target(request, owner, name)
        return RedirectResponse(url=f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)
    verify_csrf(request, form_token=csrf)
    repo = (
        (
            await db.execute(
                select(Repo)
                .where(Repo.name == name)
                .where(Repo.kind == "space")
                .options(selectinload(Repo.owner))
                .join(Repo.owner)
                .where(User.username == owner)
            )
        )
        .scalars()
        .one_or_none()
    )
    if repo is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "space not found"},
        )
    await _owner_or_403(repo, user)
    settings: Settings = request.app.state.settings
    if not bool(getattr(settings, "spaces_runtime_enabled", False)):
        target = _space_runtime_redirect_target(request, owner, name)
        return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)

    from outo_models.server.routers.spaces import _load_owner_gpu_ids, _run_lifecycle

    owner_username = repo.owner.username if repo.owner is not None else owner
    with contextlib.suppress(Exception):
        await _run_lifecycle(
            db=db,
            user=user,
            settings=settings,
            manager=SpaceRuntimeManager(settings),
            repo=repo,
            action="start",
            audit_target_id=str(repo.id),
            gpu_ids=await _load_owner_gpu_ids(db, owner_username),
        )
    target = _space_runtime_redirect_target(request, owner, name)
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{owner}/{name}/space/stop")
async def space_stop_form(
    request: Request,
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    csrf: Annotated[str | None, Form(alias=CSRF_COOKIE)] = None,
) -> Response:
    if user is None:
        target = _space_runtime_redirect_target(request, owner, name)
        return RedirectResponse(url=f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)
    verify_csrf(request, form_token=csrf)
    repo = (
        (
            await db.execute(
                select(Repo)
                .where(Repo.name == name)
                .where(Repo.kind == "space")
                .options(selectinload(Repo.owner))
                .join(Repo.owner)
                .where(User.username == owner)
            )
        )
        .scalars()
        .one_or_none()
    )
    if repo is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "space not found"},
        )
    await _owner_or_403(repo, user)
    settings: Settings = request.app.state.settings
    if not bool(getattr(settings, "spaces_runtime_enabled", False)):
        target = _space_runtime_redirect_target(request, owner, name)
        return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)

    from outo_models.server.routers.spaces import _run_lifecycle

    with contextlib.suppress(Exception):
        await _run_lifecycle(
            db=db,
            user=user,
            settings=settings,
            manager=SpaceRuntimeManager(settings),
            repo=repo,
            action="stop",
            audit_target_id=str(repo.id),
            gpu_ids=[],
        )
    target = _space_runtime_redirect_target(request, owner, name)
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["router", "templates"]
