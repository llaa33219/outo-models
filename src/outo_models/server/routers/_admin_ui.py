"""Admin dashboard page context: tab data, filters, and flash parsing.

The route handlers live in `ui.py` (they must be registered before the
`/{owner}/{name}` catch-all); the read-side plumbing they need lives
here so `ui.py` keeps only the three endpoints. Same split as
`_admin_helpers` (domain writes) and `_ui_helpers` (CSRF).

Every flash is read from the redirect query string in Python, never in
Jinja: `<noun>_error` carries the machine code, `message` its human
text, `<noun>_saved=1` a success marker.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.status import HTTP_303_SEE_OTHER

from outo_models.auth import list_pending
from outo_models.db import AuditLog, User, UserQuota, UserUsage, WebSetting
from outo_models.exceptions import ValidationFailedError
from outo_models.server.routers._admin_helpers import (
    decode_gpu_ids,
    gpu_setting_key,
)

ADMIN_TABS: tuple[str, ...] = ("signups", "users", "audit")

#: Rows per page in the users tab. A fixed size keeps the page render
#: and the batched quota / GPU lookups predictable.
ADMIN_PAGE_SIZE = 50

#: Cap on the human message echoed through a flash query, and on the
#: operator-supplied deny / ban reason.
ADMIN_MESSAGE_MAX = 240
ADMIN_REASON_MAX = 500

ADMIN_STATUSES: tuple[str, ...] = ("approved", "pending", "denied", "banned")

ADMIN_INTERNAL_ERROR = "The admin action failed. Check the server logs for details."

#: action → (tab to return to, flash noun); one table so they cannot drift.
ADMIN_ACTION_TARGET: dict[str, tuple[str, str]] = {
    "approve": ("signups", "signup"),
    "deny": ("signups", "signup"),
    "ban": ("users", "user"),
    "unban": ("users", "user"),
    "quota": ("users", "quota"),
    "gpu": ("users", "gpu"),
}

_FLASH_NOUNS: tuple[str, ...] = ("signup", "user", "quota", "gpu")


def human_bytes_for_page(n: int) -> str:
    """Byte count → `500.0 GiB`.

    `ui.py` imports this module at load time, so the formatter is
    resolved lazily to keep the cycle from becoming an import error.
    """
    from outo_models.server.routers.ui import _human_bytes

    return _human_bytes(n)


def not_found() -> JSONResponse:
    """404 in the same JSON shape every unknown repo page returns."""
    return JSONResponse(status_code=404, content={"error": "not_found", "message": "not found"})


def clean_reason(reason: str | None) -> str | None:
    """Normalize an optional deny / ban reason, rejecting an over-long one.

    Mirrors the REST body's `Field(max_length=500)` — same limit, but the
    HTML surface reports it as a flash instead of a 422 body.
    """
    cleaned = (reason or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > ADMIN_REASON_MAX:
        raise ValidationFailedError(f"reason must be at most {ADMIN_REASON_MAX} characters")
    return cleaned


def error_redirect(action: str, code: str, message: str) -> RedirectResponse:
    """303 back to `action`'s tab with `?<noun>_error=<code>&message=…`.

    `code` is the machine contract (`OutoError.code`, or
    `internal_error`); `message` is its human text, whitespace-collapsed
    and capped so a long domain error cannot blow up the URL. Both are
    URL-quoted.
    """
    tab, noun = ADMIN_ACTION_TARGET[action]
    collapsed = " ".join(message.split())[:ADMIN_MESSAGE_MAX]
    query = f"{noun}_error={quote(code, safe='')}&message={quote(collapsed, safe='')}"
    return RedirectResponse(url=f"/admin/{tab}?{query}", status_code=HTTP_303_SEE_OTHER)


def saved_redirect(action: str) -> RedirectResponse:
    """303 back to `action`'s tab with `?<noun>_saved=1`."""
    tab, noun = ADMIN_ACTION_TARGET[action]
    return RedirectResponse(url=f"/admin/{tab}?{noun}_saved=1", status_code=HTTP_303_SEE_OTHER)


def _int_param(raw: str | None, *, default: int, low: int, high: int) -> int:
    """Clamp a query-string integer into `[low, high]`, falling back."""
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return max(low, min(high, value))


def _flash_state(params: Any, noun: str) -> dict[str, Any]:
    """Read one noun's flash out of the query string.

    The human message is only meaningful next to an error code — a bare
    `?message=` from a hand-typed URL must not surface as an
    unattributed banner.
    """
    code = params.get(f"{noun}_error")
    return {
        f"{noun}_error": code,
        f"{noun}_message": (params.get("message") or "") if code else "",
        f"{noun}_saved": params.get(f"{noun}_saved") == "1",
    }


async def status_counts(db: AsyncSession) -> dict[str, int]:
    """One grouped `SELECT` → `{status: count}` for every known status."""
    rows = (await db.execute(select(User.status, func.count()).group_by(User.status))).all()
    counts = dict.fromkeys(ADMIN_STATUSES, 0)
    for user_status, count in rows:
        if user_status in counts:
            counts[user_status] = count
    return counts


async def page_context(request: Request, db: AsyncSession, tab: str) -> dict[str, Any]:
    """Build the full template context for one admin tab.

    Only the panel on screen is loaded, so opening the users tab never
    pays for the pending queue or the audit feed.
    """
    context: dict[str, Any] = {
        "tab": tab,
        "tab_signups_url": "/admin/signups",
        "tab_users_url": "/admin/users",
        "tab_audit_url": "/admin/audit",
        "tab_signups_active": tab == "signups",
        "tab_users_active": tab == "users",
        "tab_audit_active": tab == "audit",
        "status_counts": await status_counts(db),
    }
    for noun in _FLASH_NOUNS:
        context.update(_flash_state(request.query_params, noun))
    if tab == "signups":
        context["pending_users"] = await list_pending(db)
    elif tab == "users":
        context.update(await _users_panel(request, db))
    else:
        context.update(await _audit_panel(request, db))
    return context


async def _users_panel(request: Request, db: AsyncSession) -> dict[str, Any]:
    """Users tab: one paged query plus three batched per-row lookups.

    N+1 is the failure mode here — quota, usage and GPU settings are
    fetched with three set-based queries keyed on the page's user ids,
    never one query per row.
    """
    params = request.query_params
    raw_status = (params.get("status") or "").strip()
    status_filter = raw_status if raw_status in ADMIN_STATUSES else ""

    total_stmt = select(func.count()).select_from(User)
    page_stmt = select(User).order_by(User.id)
    if status_filter:
        total_stmt = total_stmt.where(User.status == status_filter)
        page_stmt = page_stmt.where(User.status == status_filter)
    total = int((await db.execute(total_stmt)).scalar_one())
    users_pages = max(1, -(-total // ADMIN_PAGE_SIZE))
    users_page = _int_param(params.get("page"), default=1, low=1, high=users_pages)
    offset = (users_page - 1) * ADMIN_PAGE_SIZE
    users = (await db.execute(page_stmt.offset(offset).limit(ADMIN_PAGE_SIZE))).scalars().all()

    return {
        "users": await _user_rows(db, users),
        "users_total": total,
        "users_page": users_page,
        "users_pages": users_pages,
        "status_filter": status_filter,
    }


async def _user_rows(db: AsyncSession, users: Sequence[User]) -> list[dict[str, Any]]:
    """Merge quota / usage / GPU lookups into one row dict per user."""
    if not users:
        return []
    user_ids = [u.id for u in users]
    quotas = {
        row.user_id: row.max_bytes
        for row in (
            await db.execute(select(UserQuota).where(UserQuota.user_id.in_(user_ids)))
        ).scalars()
    }
    usage = {
        row.user_id: row.used_bytes
        for row in (
            await db.execute(select(UserUsage).where(UserUsage.user_id.in_(user_ids)))
        ).scalars()
    }
    gpu_rows = (
        await db.execute(
            select(WebSetting).where(
                WebSetting.key.in_([gpu_setting_key(u.username) for u in users])
            )
        )
    ).scalars()
    gpus = {row.key: decode_gpu_ids(row.value) for row in gpu_rows}
    return [
        {
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "status": u.status,
            "created_at": u.created_at,
            "quota_max_bytes": quotas.get(u.id, 0),
            "quota_max_human": human_bytes_for_page(quotas.get(u.id, 0)),
            "quota_used_bytes": usage.get(u.id, 0),
            "quota_used_human": human_bytes_for_page(usage.get(u.id, 0)),
            "gpu_ids": gpus.get(gpu_setting_key(u.username), []),
        }
        for u in users
    ]


async def _audit_panel(request: Request, db: AsyncSession) -> dict[str, Any]:
    """Audit tab: newest-first rows, filterable on the indexed `action`."""
    params = request.query_params
    known_actions = (
        (await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action)))
        .scalars()
        .all()
    )
    raw_action = (params.get("action") or "").strip()
    action_filter = raw_action if raw_action in known_actions else ""
    audit_limit = _int_param(params.get("limit"), default=100, low=1, high=500)

    stmt = (
        select(AuditLog, User.username)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(audit_limit)
    )
    if action_filter:
        stmt = stmt.where(AuditLog.action == action_filter)
    rows = (await db.execute(stmt)).all()
    return {
        "audit_rows": [
            {
                "created_at": row.created_at,
                "action": row.action,
                "actor_username": actor_username,
                "target": f"{row.target_type}#{row.target_id}" if row.target_id else "",
                "detail": row.detail or "",
                "ip": row.ip or "",
            }
            for row, actor_username in rows
        ],
        "audit_limit": audit_limit,
        "audit_action_filter": action_filter,
        "audit_actions": known_actions,
    }


__all__ = [
    "ADMIN_ACTION_TARGET",
    "ADMIN_PAGE_SIZE",
    "ADMIN_REASON_MAX",
    "ADMIN_TABS",
    "clean_reason",
    "error_redirect",
    "human_bytes_for_page",
    "not_found",
    "page_context",
    "saved_redirect",
    "status_counts",
]
