"""Spaces CRUD endpoints and the Podman-backed runtime (v2).

# allow: SIZE_OK — the file spans Spaces CRUD + lifecycle + the public
# reverse-proxy; each surface owns its private helpers but they share
# schema + dependency closures (DB / settings). Splitting would force
# shared Pydantic models out of `_admin_helpers.py`-style seams and
# duplicate the import graph that mounts the two routers from a single
# `include_router(router)` in `create_app`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.config import Settings
from outo_models.db import AuditLog, Repo, User, WebSetting
from outo_models.exceptions import ForbiddenError, NotFoundError, OutoError, ValidationFailedError
from outo_models.repos.models import Visibility
from outo_models.repos.storage import REPO_LOCKS, repo_fs_path
from outo_models.server.deps import get_current_user, get_current_user_optional, get_db
from outo_models.spaces import (
    SUPPORTED_SDKS,
    RuntimeState,
    RuntimeStatus,
    SpaceRuntimeManager,
    create_space,
    delete_space,
    export_static_site,
    get_space,
    list_spaces,
    read_space_meta,
    static_site_dir,
    update_space,
)
from outo_models.spaces import (
    runtime_status as runtime_status_async,
)
from outo_models.utils.git_url import clone_url
from outo_models.utils.slug import validate_slug

router = APIRouter()
api_router = APIRouter(prefix="/api/spaces", tags=["spaces"])
proxy_router = APIRouter(tags=["spaces-proxy"])

router.include_router(api_router)
router.include_router(proxy_router)


_HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    sdk: str = "static"
    visibility: Visibility = Visibility.PRIVATE
    description: str | None = Field(default=None, max_length=500)


class SpaceSummary(BaseModel):
    id: int
    name: str
    sdk: str
    visibility: str
    description: str | None
    owner: str
    clone_url: str
    created_at: str


class RuntimeBlock(BaseModel):
    state: str
    message: str
    url: str | None
    container_id: str | None
    port: int | None


class SpaceDetail(SpaceSummary):
    runtime: RuntimeBlock


class PatchSpaceRequest(BaseModel):
    visibility: Visibility | None = None
    description: str | None = Field(default=None, max_length=500)


def _summary(row: Repo) -> SpaceSummary:
    sdk = read_space_meta(row.owner.username, row.name).sdk if row.owner else "static"
    return SpaceSummary(
        id=row.id,
        name=row.name,
        sdk=sdk,
        visibility=row.visibility,
        description=row.description,
        owner=row.owner.username if row.owner else "",
        clone_url=clone_url(row.owner.username, row.name) if row.owner else "",
        created_at=row.created_at.isoformat(),
    )


def _viewer_can_see(viewer: User | None, row: Repo) -> bool:
    if row.visibility == Visibility.PUBLIC.value:
        return True
    if viewer is None:
        return False
    return viewer.id == row.owner_id or viewer.role == "admin"


def _runtime_disabled_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": "runtime_disabled",
            "message": (
                "Runtime is disabled. An administrator must set "
                "OUTO_SPACES_RUNTIME_ENABLED=true; please try again."
            ),
        },
    )


def _settings_from(request: Request) -> Settings:
    settings = request.app.state.settings
    if not isinstance(settings, Settings):
        raise NotFoundError("Settings are not configured for this request")
    return settings


def _ensure_runtime_enabled(settings: Settings) -> JSONResponse | None:
    if settings.spaces_runtime_enabled:
        return None
    return _runtime_disabled_response()


async def _load_owner_gpu_ids(db: AsyncSession, username: str) -> list[str]:
    row = (
        await db.execute(
            select(WebSetting).where(WebSetting.key == f"gpu:{username}")
        )
    ).scalar_one_or_none()
    if row is None:
        return []
    try:
        decoded = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [s for s in decoded if isinstance(s, str)]


def _runtime_status_block(status_obj: RuntimeStatus) -> dict[str, object]:
    return {
        "state": status_obj.state.value,
        "message": status_obj.message,
        "url": status_obj.url,
        "container_id": status_obj.container_id,
        "port": status_obj.port,
    }


@api_router.post("", response_model=SpaceSummary, status_code=status.HTTP_201_CREATED)
async def create_space_route(
    body: CreateSpaceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SpaceSummary:
    if body.sdk not in SUPPORTED_SDKS:
        raise NotFoundError(f"unsupported sdk: {body.sdk!r}")
    repo = await create_space(
        db,
        owner=user,
        name=body.name,
        sdk=body.sdk,
        visibility=body.visibility,
        description=body.description,
    )
    await db.commit()
    await db.refresh(repo)
    reloaded = (
        await db.execute(
            select(Repo)
            .where(Repo.id == repo.id)
            .options(selectinload(Repo.owner))
        )
    ).scalar_one()
    return _summary(reloaded)


@api_router.get("", response_model=list[SpaceSummary])
async def list_spaces_route(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    owner: str | None = Query(default=None),
) -> list[SpaceSummary]:
    is_admin = viewer is not None and viewer.role == "admin"
    include_private = False
    if owner is not None:
        validate_slug(owner)
        include_private = is_admin or (
            viewer is not None and viewer.username == owner
        )
    rows = await list_spaces(
        db,
        owner_name=owner,
        include_private=include_private,
    )
    if rows:
        ids = [r.id for r in rows]
        owners = {
            row.id: row
            for row in (
                await db.execute(
                    select(Repo)
                    .where(Repo.id.in_(ids))
                    .options(selectinload(Repo.owner))
                )
            ).scalars().all()
        }
        for row in rows:
            fresh = owners.get(row.id)
            if fresh is not None:
                row.owner = fresh.owner
    return [_summary(r) for r in rows]


@api_router.get("/{owner}/{name}", response_model=SpaceDetail)
async def get_space_route(
    owner: str,
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    request: Request,
) -> SpaceDetail:
    repo = await get_space(db, owner_name=owner, name=name)
    if not _viewer_can_see(viewer, repo):
        raise NotFoundError(f"space not found: {owner}/{name}")
    sdk = read_space_meta(owner, name).sdk
    settings = _settings_from(request)
    status_obj = await runtime_status_async(
        repo,
        settings=settings,
        manager=SpaceRuntimeManager(settings),
    )
    return SpaceDetail(
        id=repo.id,
        name=repo.name,
        sdk=sdk,
        visibility=repo.visibility,
        description=repo.description,
        owner=repo.owner.username if repo.owner else owner,
        clone_url=clone_url(repo.owner.username, repo.name) if repo.owner else "",
        created_at=repo.created_at.isoformat(),
        runtime=RuntimeBlock(
            state=status_obj.state.value,
            message=status_obj.message,
            url=status_obj.url,
            container_id=status_obj.container_id,
            port=status_obj.port,
        ),
    )


@api_router.patch("/{owner}/{name}", response_model=SpaceSummary)
async def patch_space_route(
    owner: str,
    name: str,
    body: PatchSpaceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SpaceSummary:
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may modify this space")
    updated = await update_space(
        db,
        space=repo,
        visibility=body.visibility,
        description=body.description,
    )
    await db.commit()
    await db.refresh(updated)
    return _summary(updated)


@api_router.delete("/{owner}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space_route(
    owner: str,
    name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    validate_slug(owner)
    validate_slug(name)
    target_user = (
        await db.execute(select(User).where(User.username == owner))
    ).scalar_one_or_none()
    if target_user is None:
        raise NotFoundError(f"user {owner!r} not found")
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may delete this space")
    await delete_space(db, owner=target_user, name=name)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _run_lifecycle(
    *,
    db: AsyncSession,
    user: User,
    settings: Settings,
    manager: SpaceRuntimeManager,
    repo: Repo,
    action: str,
    audit_target_id: str,
    gpu_ids: list[str],
) -> RuntimeStatus:
    owner_name = repo.owner.username if repo.owner is not None else ""
    space_name = repo.name
    sdk = read_space_meta(owner_name, space_name).sdk
    run_url = f"{settings.base_url}/spaces/{owner_name}/{space_name}/run/"

    async def _do_action() -> RuntimeStatus:
        if action == "stop":
            await manager.stop(owner_name, space_name)
            return await runtime_status_async(
                repo, settings=settings, manager=manager
            )
        if action == "restart":
            if sdk == "static":
                export_static_site(
                    owner_name,
                    space_name,
                    static_site_dir(owner_name, space_name),
                )
                return RuntimeStatus(
                    state=RuntimeState.RUNNING,
                    message="The Space is running.",
                    url=run_url,
                )
            await manager.stop(owner_name, space_name)
            await manager.build_image(owner_name, space_name)
            await manager.start(owner_name, space_name, gpu_ids=gpu_ids)
            return await runtime_status_async(
                repo, settings=settings, manager=manager
            )
        if sdk == "static":
            export_static_site(
                owner_name,
                space_name,
                static_site_dir(owner_name, space_name),
            )
            return RuntimeStatus(
                state=RuntimeState.RUNNING,
                message="The Space is running.",
                url=run_url,
            )
        if sdk == "docker":
            fs_root = repo_fs_path(owner_name, space_name)
            if not any(
                (fs_root / candidate).exists()
                for candidate in ("Dockerfile", "Containerfile")
            ):
                raise ValidationFailedError(
                    "A docker SDK Space must have a Dockerfile or "
                    "Containerfile at the repository root."
                )
        await manager.build_image(owner_name, space_name)
        await manager.start(owner_name, space_name, gpu_ids=gpu_ids)
        return await runtime_status_async(
            repo, settings=settings, manager=manager
        )

    try:
        async with REPO_LOCKS.acquire(owner_name, space_name):
            result = await _do_action()
    except OutoError as exc:
        db.add(
            AuditLog(
                actor_id=user.id,
                action=f"space.{action}",
                target_type="space",
                target_id=audit_target_id,
                detail=json.dumps(
                    {"ok": False, "error_code": exc.code, "error_message": str(exc)},
                    ensure_ascii=False,
                ),
            )
        )
        await db.commit()
        raise
    db.add(
        AuditLog(
            actor_id=user.id,
            action=f"space.{action}",
            target_type="space",
            target_id=audit_target_id,
            detail=json.dumps(
                {"ok": True, "state": result.state.value}, ensure_ascii=False
            ),
        )
    )
    await db.commit()
    return result


@api_router.post("/{owner}/{name}/start")
async def start_space(
    owner: str,
    name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> JSONResponse:
    settings = _settings_from(request)
    disabled = _ensure_runtime_enabled(settings)
    if disabled is not None:
        return disabled
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may start this space")
    manager = SpaceRuntimeManager(settings)
    owner_username = repo.owner.username if repo.owner is not None else owner
    owner_gpu_ids = await _load_owner_gpu_ids(db, owner_username)
    status_obj = await _run_lifecycle(
        db=db,
        user=user,
        settings=settings,
        manager=manager,
        repo=repo,
        action="start",
        audit_target_id=str(repo.id),
        gpu_ids=owner_gpu_ids,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_runtime_status_block(status_obj),
    )


@api_router.post("/{owner}/{name}/stop")
async def stop_space(
    owner: str,
    name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> JSONResponse:
    settings = _settings_from(request)
    disabled = _ensure_runtime_enabled(settings)
    if disabled is not None:
        return disabled
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may stop this space")
    manager = SpaceRuntimeManager(settings)
    status_obj = await _run_lifecycle(
        db=db,
        user=user,
        settings=settings,
        manager=manager,
        repo=repo,
        action="stop",
        audit_target_id=str(repo.id),
        gpu_ids=[],
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_runtime_status_block(status_obj),
    )


@api_router.post("/{owner}/{name}/restart")
async def restart_space(
    owner: str,
    name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> JSONResponse:
    settings = _settings_from(request)
    disabled = _ensure_runtime_enabled(settings)
    if disabled is not None:
        return disabled
    repo = await get_space(db, owner_name=owner, name=name)
    if repo.owner_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the owner or an admin may restart this space")
    manager = SpaceRuntimeManager(settings)
    owner_username = repo.owner.username if repo.owner is not None else owner
    owner_gpu_ids = await _load_owner_gpu_ids(db, owner_username)
    status_obj = await _run_lifecycle(
        db=db,
        user=user,
        settings=settings,
        manager=manager,
        repo=repo,
        action="restart",
        audit_target_id=str(repo.id),
        gpu_ids=owner_gpu_ids,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_runtime_status_block(status_obj),
    )


@api_router.get("/{owner}/{name}/status")
async def status_route(
    owner: str,
    name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> JSONResponse:
    repo = await get_space(db, owner_name=owner, name=name)
    if not _viewer_can_see(viewer, repo):
        raise NotFoundError(f"space not found: {owner}/{name}")
    settings = _settings_from(request)
    status_obj = await runtime_status_async(
        repo,
        settings=settings,
        manager=SpaceRuntimeManager(settings),
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_runtime_status_block(status_obj),
    )


def _safe_site_path(base: Path, requested: str) -> Path | None:
    candidate = (base / requested).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    if not candidate.exists() or candidate.is_dir():
        return None
    return candidate


def _file_response_for_static(site_root: Path, path: str) -> Response | None:
    if not site_root.exists():
        return None
    if not path or path.endswith("/"):
        path = "index.html"
    if path.startswith("/"):
        path = path[1:]
    target = _safe_site_path(site_root, path)
    if target is None:
        return None
    return FileResponse(target)


def _strip_hop_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


async def _stream_proxy_response(
    method: str,
    target_url: str,
    body: bytes,
    headers: dict[str, str],
) -> StreamingResponse | JSONResponse:
    """Reverse-proxy `body` at `method` to `target_url` and stream back.

    The function reads `upstream.content` eagerly into a `StreamingResponse`
    iterator. Using the raw bytes iterator would require per-chunk awaits
    which the test client cannot drive under `httpx.MockTransport`; the
    eager approach keeps the API contract intact while staying
    deterministically mockable.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
    ) as proxy_client:
        try:
            upstream = await proxy_client.request(
                method,
                target_url,
                content=body,
                headers=headers,
            )
            payload = await upstream.aread()
        except httpx.RequestError as exc:
            return JSONResponse(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                content={
                    "error": "proxy_unreachable",
                    "message": f"Cannot connect to the Space container: {exc}",
                },
            )
        cleaned = _strip_hop_headers(upstream.headers)
        return StreamingResponse(
            iter([payload]),
            status_code=upstream.status_code,
            headers=cleaned,
            media_type=upstream.headers.get("content-type"),
        )


@proxy_router.get("/spaces/{owner}/{name}/run/{path:path}")
async def proxy_run_get(
    owner: str,
    name: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    return await _proxy_dispatch(owner, name, path, "GET", b"", request, db, viewer)


@proxy_router.post("/spaces/{owner}/{name}/run/{path:path}")
async def proxy_run_post(
    owner: str,
    name: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    body = await request.body()
    return await _proxy_dispatch(owner, name, path, "POST", body, request, db, viewer)


@proxy_router.put("/spaces/{owner}/{name}/run/{path:path}")
async def proxy_run_put(
    owner: str,
    name: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    body = await request.body()
    return await _proxy_dispatch(owner, name, path, "PUT", body, request, db, viewer)


@proxy_router.patch("/spaces/{owner}/{name}/run/{path:path}")
async def proxy_run_patch(
    owner: str,
    name: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    body = await request.body()
    return await _proxy_dispatch(owner, name, path, "PATCH", body, request, db, viewer)


@proxy_router.delete("/spaces/{owner}/{name}/run/{path:path}")
async def proxy_run_delete(
    owner: str,
    name: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    body = await request.body()
    return await _proxy_dispatch(owner, name, path, "DELETE", body, request, db, viewer)


async def _proxy_dispatch(
    owner: str,
    name: str,
    path: str,
    method: str,
    body: bytes,
    request: Request,
    db: AsyncSession,
    viewer: User | None,
) -> Response:
    repo = await get_space(db, owner_name=owner, name=name)
    if not _viewer_can_see(viewer, repo):
        raise NotFoundError(f"space not found: {owner}/{name}")
    settings = request.app.state.settings
    sdk = read_space_meta(owner, name).sdk
    if sdk == "static":
        site_root = static_site_dir(owner, name)
        served = _file_response_for_static(site_root, path)
        if served is not None:
            return served
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "not_found",
                "message": f"File not found: {path!r}",
            },
        )
    if not settings.spaces_runtime_enabled:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "runtime_disabled",
                "message": "Runtime is disabled.",
            },
        )
    manager = SpaceRuntimeManager(settings)
    payload = await manager.inspect(owner, name)
    host_port = _host_port_from_inspect(payload)
    if host_port is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "space_not_running",
                "message": (
                    "The Space is not running. Please start it and try again."
                ),
            },
        )
    target_url = f"http://127.0.0.1:{host_port}/{path}"
    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return await _stream_proxy_response(method, target_url, body, forward_headers)


def _host_port_from_inspect(
    payload: dict[str, object] | None,
) -> int | None:
    if not isinstance(payload, dict):
        return None
    state_obj = payload.get("State")
    if not isinstance(state_obj, dict):
        return None
    status_value = state_obj.get("Status")
    if not (isinstance(status_value, str) and status_value.lower() == "running"):
        return None
    ns = payload.get("NetworkSettings")
    if not isinstance(ns, dict):
        return None
    ports = ns.get("Ports")
    if not isinstance(ports, dict) or not ports:
        return None
    candidates: list[int] = []
    for bindings in ports.values():
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            hp = binding.get("HostPort")
            if hp is None:
                continue
            try:
                candidates.append(int(hp))
            except (TypeError, ValueError):
                continue
    return min(candidates) if candidates else None


__all__ = ["proxy_router", "restart_space", "router", "start_space", "stop_space"]
