"""Server-side multipart file upload endpoint.

`POST /api/repos/{owner}/{name}/upload` accepts a multipart form with:

    * `message` (optional) — commit message
    * `path` (optional) — directory prefix the files are written under
    * `files[]` — one or more file parts; each part's filename may
      contain a relative subpath (e.g. `sub/dir/weights.bin`)

The endpoint authenticates via either the session cookie OR a Bearer PAT
that carries the `write` scope. Only the repo owner or an admin may
upload; the status matrix mirrors the visibility rule used everywhere
else:

    * anonymous                 -> 401
    * authenticated, not owner  -> 403
    * repo missing or invisible -> 404 (don't leak existence)

Behaviour:

    1. Per-file size cap (100 MiB) -- 413 with a hint to use git+LFS for
       larger objects.
    2. Path-traversal + absolute-path rejection (`..`, leading `/`)
       returns 422.
    3. Quota pre-check (`check_push_allowed`) on the sum of incoming
       bytes -- 413 with the same LFS hint if it would push the user over
       the cap.
    4. Files are committed via `repos.commit.commit_files` under the
       per-repo `RepoLockRegistry` write lock so a concurrent push
       cannot interleave.
    5. `Revision` + `AuditLog(action="repo.upload")` rows are written;
       `Repo.size_bytes` and `UserUsage.used_bytes` are refreshed.
    6. Response: `{"commit_sha", "files", "message"}`.

Auth scope checks, path validation, and PAT scope decoding live in the
sibling `_upload_helpers.py` so this file stays focused on the route
contract. The split mirrors the existing `_auth_helpers.py` /
`_resolve_helpers.py` pattern in the routers package.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.config import Settings, get_settings
from outo_models.db import AuditLog, Repo, Revision, User
from outo_models.exceptions import (
    ForbiddenError,
    NotFoundError,
    QuotaExceededError,
    UnauthorizedError,
    ValidationFailedError,
)
from outo_models.repos.commit import commit_files
from outo_models.repos.quota import add_usage, check_push_allowed
from outo_models.repos.storage import disk_usage, repo_fs_path
from outo_models.server.deps import get_db
from outo_models.server.routers._upload_helpers import (
    MAX_FILE_BYTES,
    resolve_principal,
    validate_filename,
    validate_path,
)

router = APIRouter(prefix="/api/repos", tags=["upload"])


@router.post("/{owner}/{name}/upload")
async def upload_files(
    owner: str,
    name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    files: Annotated[list[UploadFile] | None, File()] = None,  # FastAPI form-list binding
    message: Annotated[str, Form()] = "",
    path: Annotated[str, Form()] = "",
) -> dict[str, object]:
    """Commit `files[]` into `<owner>/<name>`'s default branch."""
    del settings  # resolved through request inside resolve_principal
    if files is None:
        files = []

    user = await resolve_principal(db=db, request=request)
    if user is None:
        raise UnauthorizedError("Authentication required for upload")

    repo_row = (
        await db.execute(
            select(Repo)
            .where(Repo.name == name)
            .options(selectinload(Repo.owner))
            .join(Repo.owner)
            .where(User.username == owner)
        )
    ).scalar_one_or_none()
    if repo_row is None:
        raise NotFoundError(f"repository not found: {owner}/{name}")

    if not (repo_row.owner_id == user.id or user.role == "admin"):
        raise ForbiddenError("Only the owner or an admin may upload to this repo")

    if not files:
        raise ValidationFailedError("at least one file is required")

    prefix_segments = validate_path(path)

    collected: dict[str, bytes] = {}
    incoming_bytes = 0
    for part in files:
        cleaned = validate_filename(part.filename)
        joined_segments = prefix_segments + cleaned.split("/")
        if any(seg in (".", "..") or not seg for seg in joined_segments):
            raise ValidationFailedError(f"invalid path: {cleaned!r}")
        content = await part.read()
        if len(content) > MAX_FILE_BYTES:
            raise QuotaExceededError(
                f"file {cleaned!r} exceeds per-file limit "
                f"{MAX_FILE_BYTES} bytes; use git+lfs for larger files"
            )
        collected[cleaned] = content
        incoming_bytes += len(content)

    owner_user = (await db.execute(select(User).where(User.id == repo_row.owner_id))).scalar_one()
    try:
        await check_push_allowed(db, owner_user, incoming_bytes)
        await db.commit()
    except QuotaExceededError as exc:
        raise QuotaExceededError(
            f"{exc} -- for large objects use git+lfs instead of the upload endpoint"
        ) from exc

    fs_path = repo_fs_path(owner, name)
    try:
        result = await commit_files(
            owner=owner,
            name=name,
            default_branch=repo_row.default_branch,
            actor_username=user.username,
            actor_email=user.email,
            files=collected,
            prefix=path,
            message=message,
        )
    except FileNotFoundError as exc:
        raise NotFoundError(f"repository not found: {owner}/{name}") from exc

    fs_size = await disk_usage(fs_path)
    size_delta = fs_size - repo_row.size_bytes
    repo_row.size_bytes = fs_size

    db.add(
        Revision(
            repo_id=repo_row.id,
            commit_sha=result.commit_sha,
            branch=repo_row.default_branch,
            author_id=user.id,
            message=message or f"upload: {len(result.paths)} file(s)",
            size_bytes=incoming_bytes,
        )
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="repo.upload",
            target_type="repo",
            target_id=str(repo_row.id),
            detail=json.dumps(
                {
                    "repo": f"{owner}/{name}",
                    "commit_sha": result.commit_sha,
                    "files": result.paths,
                    "bytes": incoming_bytes,
                }
            ),
        )
    )
    await add_usage(db, owner_user, size_delta)
    await db.commit()

    return {
        "commit_sha": result.commit_sha,
        "files": result.paths,
        "message": message or f"upload: {len(result.paths)} file(s)",
    }


__all__ = ["router"]
