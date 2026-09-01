"""Admin-only REST surface for the WP-14 CLI and any future operator UI.

Authorization: every endpoint requires `role == "admin"`. The `require_admin`
dependency handles the 401/403 cascade; this module only translates CLI
input into domain calls and AuditLog rows.

State transitions that are already audited (`ban_user`, `unban_user`,
`approve_user`, `deny_user`) flow through `outo_models.auth.approval` so
they keep writing their `AuditLog` rows. Admin operations outside that
state machine (quota / gpu assignments) get their own `AuditLog`
entries emitted via `_admin_helpers.write_admin_audit`.

# allow: SIZE_OK — the WP-13 contract lists 9 admin endpoints + 3 schemas
# as the surface, and splitting the file would force the test imports +
# Pydantic model references to be threaded through a separate module.
# Keeping it together is the smaller diff.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.auth import (
    approve_user,
    ban_user,
    deny_user,
    unban_user,
)
from outo_models.db import AuditLog, User, WebSetting
from outo_models.repos.quota import ensure_quota_rows
from outo_models.server.deps import get_db, require_admin
from outo_models.server.routers._admin_helpers import (
    get_gpu_assignments,
    gpu_setting_key,
    load_target_user,
    quota_dict,
    write_admin_audit,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class QuotaUpdateRequest(BaseModel):
    """PUT /api/admin/users/{username}/quota body."""

    max_bytes: int = Field(gt=0, le=10**15)


class GPUAssignRequest(BaseModel):
    """PUT /api/admin/users/{username}/gpu body."""

    gpu_ids: list[str] = Field(default_factory=list)


class DenyRequest(BaseModel):
    """Optional reason for deny / ban actions."""

    reason: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# User listing
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(pending|approved|denied|banned)?$"
    ),
) -> list[dict[str, object]]:
    """List every user, optionally filtered by `status`."""
    stmt = select(User).order_by(User.id)
    if status_filter:
        stmt = stmt.where(User.status == status_filter)
    users = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "status": u.status,
            "display_name": u.display_name,
            "created_at": u.created_at.isoformat(),
            "approved_at": u.approved_at.isoformat() if u.approved_at else None,
            "quota": await quota_dict(db, u),
        }
        for u in users
    ]


# ---------------------------------------------------------------------------
# Approval state machine (delegates to `outo_models.auth.approval`)
# ---------------------------------------------------------------------------


@router.post("/users/{username}/approve")
async def approve(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """Approve a pending user."""
    approved = await approve_user(db, username=username, admin=admin)
    await db.commit()
    return {
        "username": approved.username,
        "status": approved.status,
        "approved_at": approved.approved_at.isoformat() if approved.approved_at else None,
    }


@router.post("/users/{username}/deny")
async def deny(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: DenyRequest | None = None,
) -> dict[str, object]:
    """Deny a pending user."""
    reason = body.reason if body is not None else None
    denied = await deny_user(db, username=username, admin=admin, reason=reason)
    await db.commit()
    return {"username": denied.username, "status": denied.status}


@router.post("/users/{username}/ban")
async def ban(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: DenyRequest | None = None,
) -> dict[str, object]:
    """Ban any non-banned user (subject to self/admin safety rails)."""
    reason = body.reason if body is not None else None
    banned = await ban_user(db, username=username, admin=admin, reason=reason)
    await db.commit()
    return {"username": banned.username, "status": banned.status}


@router.post("/users/{username}/unban")
async def unban(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """Restore a banned user to `approved`."""
    restored = await unban_user(db, username=username, admin=admin)
    await db.commit()
    return {"username": restored.username, "status": restored.status}


# ---------------------------------------------------------------------------
# Quota management
# ---------------------------------------------------------------------------


@router.get("/users/{username}/quota")
async def get_quota(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    """Read the storage quota for `username`."""
    user = await load_target_user(db, username)
    return await quota_dict(db, user)


@router.put("/users/{username}/quota")
async def set_quota(
    username: str,
    body: QuotaUpdateRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    """Set the storage quota (in bytes) and audit the change."""
    user = await load_target_user(db, username)
    quota, _usage = await ensure_quota_rows(db, user)
    old_value = quota.max_bytes
    quota.max_bytes = body.max_bytes
    write_admin_audit(
        db,
        admin=admin,
        action="admin.quota",
        target_id=user.id,
        detail=json.dumps(
            {"old_max_bytes": old_value, "new_max_bytes": body.max_bytes},
            ensure_ascii=False,
        ),
    )
    await db.commit()
    return {"max_bytes": quota.max_bytes}


# ---------------------------------------------------------------------------
# GPU assignments (stored as WebSetting rows)
# ---------------------------------------------------------------------------


@router.get("/users/{username}/gpu")
async def get_gpu(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, list[str]]:
    """Read the GPU ids currently assigned to `username`."""
    await load_target_user(db, username)
    return {"gpu_ids": await get_gpu_assignments(db, username)}


@router.put("/users/{username}/gpu")
async def set_gpu(
    username: str,
    body: GPUAssignRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, list[str]]:
    """Replace the GPU assignment list and audit the change."""
    user = await load_target_user(db, username)
    key = gpu_setting_key(username)
    row = (await db.execute(select(WebSetting).where(WebSetting.key == key))).scalar_one_or_none()
    encoded = json.dumps(list(body.gpu_ids), ensure_ascii=False)
    if row is None:
        db.add(WebSetting(key=key, value=encoded))
    else:
        row.value = encoded
    write_admin_audit(
        db,
        admin=admin,
        action="admin.gpu",
        target_id=user.id,
        detail=encoded,
    )
    await db.commit()
    return {"gpu_ids": list(body.gpu_ids)}


@router.delete("/users/{username}/gpu", status_code=status.HTTP_204_NO_CONTENT)
async def clear_gpu(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Drop the GPU assignment list entirely."""
    user = await load_target_user(db, username)
    key = gpu_setting_key(username)
    row = (await db.execute(select(WebSetting).where(WebSetting.key == key))).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        write_admin_audit(
            db,
            admin=admin,
            action="admin.gpu",
            target_id=user.id,
            detail=json.dumps({"cleared": True}),
        )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Audit log feed (read-only)
# ---------------------------------------------------------------------------


@router.get("/audit")
async def audit_feed(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    """Return the most-recent `AuditLog` rows, newest first."""
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "actor_id": r.actor_id,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "detail": r.detail,
            "ip": r.ip,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


__all__ = ["get_gpu_assignments", "router"]
