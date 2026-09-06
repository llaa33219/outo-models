"""Raw-file download endpoint.

`GET /{owner}/{name}/resolve/{revision}/{path:path}` returns the bytes of
`path` inside `<owner>/<name>` at `revision` (branch / tag / commit sha).
The shape mirrors Hugging Face's `resolve` URLs so a CLI can fetch any
file in a repo without going through the git smart-HTTP pack protocol:

    GET https://<domain>/<owner>/<name>/resolve/<ref>/<path>

Implementation notes:

    * Auth & visibility follow the same matrix as `GET /api/repos/<o>/<n>`:
      anonymous on public repos works; private repos require a session
      cookie OR a Basic PAT (the git_smart Basic-auth resolver is reused
      for the PAT path); 404 — not 403 — for unauthorized access to
      private repos so the API does not leak existence.
    * `revision` is resolved through `repos.card.resolve_tip_sha` for
      branch names; bare sha inputs are looked up directly.
    * LFS pointer files (`version https://git-lfs` + a parseable
      `{oid, size}` line) redirect to the existing LFS GET endpoint so
      a CLI asking for an LFS blob transparently downloads the actual
      object, not the in-tree pointer text.
    * Range requests are honoured so a multi-hundred-megabyte blob can
      be resumed; the body streams in 64 KiB chunks so peak memory is
      O(chunk), independent of blob size.
    * ETag is the blob's git sha1 — the same id a `git cat-file blob`
      would print — so clients can re-validate cheaply.

The git-side machinery (blob resolution, range parsing, LFS pointer
sniff, streaming) lives in the sibling `_resolve_helpers.py` so this
file stays focused on HTTP-shape concerns. The split mirrors the
existing `_auth_helpers.py` / `_admin_helpers.py` / `_ui_helpers.py`
pattern in the routers package.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.config import Settings, get_settings
from outo_models.db import Repo, User
from outo_models.exceptions import NotFoundError
from outo_models.git_smart.auth import resolve_git_identity
from outo_models.repos.storage import repo_fs_path
from outo_models.server.deps import (
    get_current_user_optional,
    get_db,
)
from outo_models.server.routers._resolve_helpers import (
    content_type_for,
    iter_blob_window,
    maybe_lfs_pointer,
    parse_range,
    peek_blob_head,
    resolve_blob,
)

router = APIRouter(tags=["resolve"])


async def _viewer_can_see(
    *,
    db: AsyncSession,
    settings: Settings,
    request: Request,
    repo_row: Repo,
    authorization: str | None,
) -> bool:
    """Apply the same visibility rule the JSON repos router uses.

    Public repos are open to anyone; private repos require either a
    valid session cookie OR a Basic-auth PAT belonging to the owner or
    an admin. We reuse `resolve_git_identity` so the same PAT
    verification the git service uses applies here — credentials the
    server already trusts are trusted in this surface too.
    """
    if repo_row.visibility == "public":
        return True
    from outo_models.server.deps import _resolve_session_user, _settings_for_request

    sess_user_id = _resolve_session_user(request, _settings_for_request(request))
    if sess_user_id is not None:
        user = (await db.execute(select(User).where(User.id == sess_user_id))).scalar_one_or_none()
        if user is not None and user.is_active:
            return user.id == repo_row.owner_id or user.role == "admin"

    if authorization:
        user = await resolve_git_identity(authorization, settings=settings)
        if user is not None and user.is_active:
            return user.id == repo_row.owner_id or user.role == "admin"

    return False


@router.get(
    "/{owner}/{name}/resolve/{revision}/{path:path}",
)
async def resolve_file(
    owner: Annotated[str, Path(min_length=1, max_length=63)],
    name: Annotated[str, Path(min_length=1, max_length=63)],
    revision: Annotated[str, Path(min_length=1, max_length=64)],
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    range_header: Annotated[str | None, Header(alias="range")] = None,
) -> Response:
    """Return the raw bytes of `path` at `revision`.

    Streaming body for 200 / 206 paths so a multi-hundred-MB blob never
    sits in memory. Range requests are honoured via the
    `Range: bytes=start-end` header; an invalid Range yields `416`.

    LFS pointer files (detected by content sniff) redirect to the
    existing `/{owner}/{name}.git/info/lfs/objects/{oid}` endpoint so
    a CLI fetching an LFS-backed file transparently receives the actual
    object, not the in-tree pointer text.
    """
    del viewer  # visibility goes through request-level deps for parity with JSON API

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
        # Do not leak existence of private repos: 404 either way.
        raise NotFoundError(f"file not found: {owner}/{name}/resolve/{revision}/{path}")

    authorization = request.headers.get("authorization")
    if not await _viewer_can_see(
        db=db,
        settings=settings,
        request=request,
        repo_row=repo_row,
        authorization=authorization,
    ):
        raise NotFoundError(f"file not found: {owner}/{name}/resolve/{revision}/{path}")

    fs_path = repo_fs_path(owner, name)
    blob_sha, blob_size = await resolve_blob(owner, name, revision, path)

    head = await asyncio.to_thread(peek_blob_head, str(fs_path), blob_sha, max_bytes=512)
    if head is not None:
        pointer = maybe_lfs_pointer(head)
        if pointer is not None:
            target = f"/{owner}/{name}.git/info/lfs/objects/{pointer.oid}"
            return Response(status_code=302, headers={"Location": target})

    range_spec = parse_range(range_header, blob_size)
    if range_spec is not None and not range_spec.satisfiable:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{blob_size}"},
        )

    start = 0 if range_spec is None else range_spec.start
    end = blob_size - 1 if range_spec is None else range_spec.end
    length = max(0, end - start + 1)

    etag = f'"{blob_sha.decode("ascii")}"'
    content_type = content_type_for(path)

    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=300",
    }
    if range_spec is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{blob_size}"

    iterator = iter_blob_window(str(fs_path), blob_sha, start=start, end=end)

    return StreamingResponse(
        iterator,
        status_code=206 if range_spec is not None else 200,
        headers=headers,
        media_type=content_type,
    )


__all__ = ["router"]
