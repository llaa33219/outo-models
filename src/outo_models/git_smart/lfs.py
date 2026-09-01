"""Git LFS HTTP surface for the git smart-HTTP service.

This module is the *only* seam between the existing `GitSmartService`
and the LFS implementation. `lfs_dispatch` is the entry WP-13 wires
into the service in place of the old 501 stub; it routes the four LFS
endpoints to the right handler:

    POST  /info/lfs/objects/batch    → batch (upload or download)
    PUT   /info/lfs/objects/{oid}    → upload (local backend only)
    GET   /info/lfs/objects/{oid}    → download
    *     /info/lfs/locks*           → 501 (locks are not supported yet)

The dispatch loads the `(owner, repo)` row, resolves the Basic-auth
identity, applies the authorize matrix, then delegates to either
`lfs_api.handle_batch` (pure logic) or the local store's server-side
helpers. The S3 backend never reaches PUT/GET because its href points at
the S3 endpoint — the client uploads / downloads directly.

`lfs_not_supported` (the public 501 helper) is retained so the existing
unit-test file can pin the locks behavior without depending on the
dispatch internals.

# allow: SIZE_OK — the WP-15 ownership list locks this file to
# `src/outo_models/git_smart/lfs.py`, so the ASGI response helpers,
# the per-endpoint handlers, and the dispatcher cannot live in separate
# modules of their own. Splitting would force changes outside the
# owned-file list, which the task contract forbids.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy import select

from outo_models.config import Settings
from outo_models.db import AuditLog, Repo, User, get_session_factory
from outo_models.exceptions import (
    ForbiddenError,
    QuotaExceededError,
    UnauthorizedError,
    ValidationFailedError,
)
from outo_models.git_smart.auth import GitAction, authorize, resolve_git_identity
from outo_models.git_smart.lfs_api import (
    dedup_objects,
    handle_batch,
    parse_batch_body,
)
from outo_models.objectstore.factory import create_object_store
from outo_models.objectstore.local import LocalObjectStore
from outo_models.repos.quota import add_usage

#: Stable documentation URL embedded in the 501 body for unsupported endpoints.
_DOCS_PATH = "/docs/git-lfs"

#: Pre-rendered JSON body for the 501 response (locks only).
_BODY_BYTES: bytes = json.dumps(
    {"error": "Git LFS locks are not supported yet", "docs": _DOCS_PATH}
).encode("utf-8")

#: Required content type for LFS batch requests / responses.
LFS_CONTENT_TYPE = "application/vnd.git-lfs+json"

#: Cap on the batch request body — the spec is small, ~tens of KB.
_BATCH_BODY_LIMIT = 1024 * 1024  # 1 MiB; comfortable headroom.

#: Cap on a single object PUT. The store will also reject anything
# larger than `Settings.lfs_max_object_bytes` but we mirror it here so
# we don't buffer a multi-GB payload before learning it would have
# failed anyway.
#: (See PUT handler for the actual enforcement.)

#: Type aliases for the ASGI triples — kept local to avoid a circular
#: import with `service.py`.
ASGIReceive = Callable[[], Awaitable[dict[str, object]]]
ASGISend = Callable[[dict[str, object]], Awaitable[None]]


# ---------------------------------------------------------------------------
# 501 helper (locks endpoint)
# ---------------------------------------------------------------------------


async def lfs_not_supported(
    scope: object,
    receive: Callable[[], Awaitable[dict[str, object]]],
    send: ASGISend,
) -> None:
    """Reply `501 Not Implemented` with a stable JSON envelope.

    Used only for `/info/lfs/locks*` — every other LFS endpoint has a
    real implementation now. `scope` and `receive` are unused: LFS
    clients learn "not implemented" from the status code alone and stop
    retrying.
    """
    del scope, receive

    await send(
        {
            "type": "http.response.start",
            "status": 501,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_BODY_BYTES)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": _BODY_BYTES,
            "more_body": False,
        }
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _header_value(headers: object, name: str) -> str | None:
    """Return the first matching header value from ASGI headers (case-insensitive)."""
    if not isinstance(headers, list):
        return None
    needle = name.lower()
    for raw_k, raw_v in headers:
        k = raw_k.decode("latin-1").lower() if isinstance(raw_k, bytes) else str(raw_k).lower()
        if k == needle:
            return (
                raw_v.decode("latin-1") if isinstance(raw_v, bytes) else str(raw_v)
            )
    return None


async def _send_status(
    send: ASGISend,
    *,
    status: int,
    payload: Any | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    """Emit a JSON response with the LFS content type.

    `payload=None` is sent as the JSON literal `null`. Pass an empty
    dict explicitly if you want `{}`.
    """
    body = json.dumps(payload).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", LFS_CONTENT_TYPE.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        for k, v in extra_headers:
            headers.append((k.encode("latin-1"), v.encode("latin-1")))
    await send(
        {"type": "http.response.start", "status": status, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_bytes(
    send: ASGISend,
    *,
    status: int,
    body: bytes,
    content_type: str,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    """Emit a raw-bytes response (used for plain 404 / 401 / 415 bodies)."""
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode("latin-1")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        for k, v in extra_headers:
            headers.append((k.encode("latin-1"), v.encode("latin-1")))
    await send(
        {"type": "http.response.start", "status": status, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_401(send: ASGISend) -> None:
    body = b"Authentication required"
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"www-authenticate", b'Basic realm="outo-models", charset="UTF-8"'),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_403(send: ASGISend, message: str) -> None:
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_404(send: ASGISend, message: str = "not found") -> None:
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_405(send: ASGISend, allow: str) -> None:
    body = b"method not allowed"
    await send(
        {
            "type": "http.response.start",
            "status": 405,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"allow", allow.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_full_body(receive: ASGIReceive, *, limit: int) -> bytes:
    """Drain the ASGI request body; raises `ValueError` if too large.

    Bounded by `limit` (bytes); callers choose the cap based on the
    endpoint. Raises `ValueError` on overflow and `ConnectionError` on
    client disconnect so the dispatcher can map them to 413 / 499
    respectively.
    """
    chunks: list[bytes] = []
    total = 0
    more = True
    while more:
        event = await receive()
        if event.get("type") == "http.disconnect":
            raise ConnectionError("client disconnected")
        chunk = event.get("body")
        if isinstance(chunk, (bytes, bytearray)) and chunk:
            total += len(chunk)
            if total > limit:
                raise ValueError(f"request body exceeds {limit} bytes")
            chunks.append(bytes(chunk))
        more = bool(event.get("more_body", False))
    return b"".join(chunks)


async def _stream_request_body(
    receive: ASGIReceive, *, limit: int
) -> AsyncIterator[bytes]:
    """Yield the request body chunks without buffering.

    Stops when `more_body=False` OR the running total crosses `limit`
    (in which case it raises `ValueError`). The PUT handler uses this so
    a multi-GB object upload is not buffered into RAM.
    """
    total = 0
    while True:
        event = await receive()
        if event.get("type") == "http.disconnect":
            return
        chunk = event.get("body")
        if isinstance(chunk, (bytes, bytearray)) and chunk:
            total += len(chunk)
            if total > limit:
                raise ValueError(f"request body exceeds {limit} bytes")
            yield bytes(chunk)
        if not event.get("more_body", False):
            return


def _accepts_lfs(headers: object) -> bool:
    """True iff the `Accept` header mentions the LFS content type.

    Per the LFS spec the client MUST send `Accept: application/vnd.git-lfs+json`;
    a server is free to 406 a client that doesn't.
    """
    accept = _header_value(headers, "accept")
    if accept is None:
        return False
    return LFS_CONTENT_TYPE in accept


def _is_lfs_content_type(headers: object) -> bool:
    """True iff the `Content-Type` header advertises the LFS body type."""
    ct = _header_value(headers, "content-type")
    if ct is None:
        return False
    return ct.split(";", 1)[0].strip().lower() == LFS_CONTENT_TYPE


def _request_base_url(scope: dict[str, object]) -> str:
    """Derive `scheme://host[:port]` from the ASGI scope.

    Honors the `Host` header (with port) and the ASGI `scheme` field,
    so the LFS action href matches the origin the client is actually
    connecting to — including non-default ports under uvicorn tests.
    """
    raw_scheme = scope.get("scheme")
    scheme = str(raw_scheme) if isinstance(raw_scheme, str) else "http"

    raw_host = _header_value(scope.get("headers"), "host")
    if raw_host:
        return f"{scheme}://{raw_host}"

    server = scope.get("server")
    if isinstance(server, (list, tuple)) and len(server) >= 2:
        try:
            host_port = f"{server[0]}:{int(server[1])}"
        except (TypeError, ValueError):
            host_port = str(server[0])
        return f"{scheme}://{host_port}"

    return f"{scheme}://localhost"


# ---------------------------------------------------------------------------
# repo + user loading
# ---------------------------------------------------------------------------


async def _load_repo(
    *, owner_name: str, repo_name: str
) -> tuple[Repo, User] | None:
    """Load the `(Repo, owner User)` pair; `None` if the repo is missing.

    Returns `None` for missing so the dispatcher can answer 404 without
    a try / except. The owner `User` is loaded eagerly so `authorize()`
    can compare IDs without a second round-trip.
    """
    factory = get_session_factory()
    async with factory() as session:
        repo_row = (
            await session.execute(
                select(Repo).where(
                    Repo.name == repo_name,
                    Repo.owner_id == select(User.id)
                    .where(User.username == owner_name)
                    .scalar_subquery(),
                )
            )
        ).scalar_one_or_none()
        if repo_row is None:
            return None
        owner = (
            await session.execute(select(User).where(User.id == repo_row.owner_id))
        ).scalar_one()
        # Detach so the row can be used after the session closes.
        return repo_row, owner


# ---------------------------------------------------------------------------
# batch handler
# ---------------------------------------------------------------------------


async def _handle_batch(
    scope: dict[str, object],
    receive: ASGIReceive,
    send: ASGISend,
    *,
    settings: Settings,
    owner_name: str,
    repo_name: str,
) -> None:
    """Serve `POST /info/lfs/objects/batch`.

    Requires:
        - `Accept: application/vnd.git-lfs+json` (else 406).
        - `Content-Type: application/vnd.git-lfs+json` (else 415).
        - Repo exists (else 404).
        - For `upload`: authenticated owner / admin (else 401 / 403).
        - For `download`: owner / admin OR public visibility (else 401 / 403).

    The body is read first so we know the operation (`upload` vs
    `download`) before applying the auth gate — anonymous PULL of a
    public repo must NOT be 401'd.
    """
    headers = scope.get("headers")
    if not _accepts_lfs(headers):
        await _send_bytes(
            send,
            status=406,
            body=json.dumps(
                {"error": "Accept must include application/vnd.git-lfs+json"}
            ).encode("utf-8"),
            content_type="application/json",
        )
        return
    if not _is_lfs_content_type(headers):
        await _send_bytes(
            send,
            status=415,
            body=json.dumps(
                {"error": "Content-Type must be application/vnd.git-lfs+json"}
            ).encode("utf-8"),
            content_type="application/json",
        )
        return

    try:
        raw = await _read_full_body(receive, limit=_BATCH_BODY_LIMIT)
    except ValueError:
        await _send_status(
            send, status=413, payload={"error": "batch body too large"}
        )
        return
    except ConnectionError:
        return

    try:
        request = parse_batch_body(raw)
    except ValidationFailedError as exc:
        await _send_status(send, status=422, payload={"error": str(exc)})
        return

    repo_pair = await _load_repo(owner_name=owner_name, repo_name=repo_name)
    if repo_pair is None:
        await _send_404(send, "repository not found")
        return
    repo_row, owner = repo_pair

    action = (
        GitAction.PUSH if request.operation == "upload" else GitAction.PULL
    )
    auth_header = _header_value(headers, "authorization")
    user = await resolve_git_identity(auth_header, settings=settings)
    try:
        await authorize(user, repo=repo_row, owner=owner, action=action)
    except UnauthorizedError:
        await _send_401(send)
        return
    except ForbiddenError as exc:
        await _send_403(send, str(exc) or "forbidden")
        return

    # Dedup — same (oid, size) pair → single response entry.
    request.objects = dedup_objects(request.objects)

    # Pick the store.
    try:
        store = create_object_store(settings, base_url_override=_request_base_url(scope))
    except Exception as exc:  # ConfigError, etc.
        await _send_status(send, status=500, payload={"error": str(exc)})
        return

    factory = get_session_factory()
    async with factory() as session:
        try:
            response = await handle_batch(
                request=request,
                store=store,
                owner_name=owner_name,
                repo_name=repo_name,
                actor_id=user.id if user is not None else None,
                session=session,
                settings=settings,
            )
            await session.commit()
        except QuotaExceededError as exc:
            # Should not escape handle_batch — defensive.
            await _send_status(send, status=413, payload={"error": str(exc)})
            return
        except ValidationFailedError as exc:
            await _send_status(send, status=422, payload={"error": str(exc)})
            return

    await _send_status(send, status=200, payload=response.to_wire())


# ---------------------------------------------------------------------------
# object PUT (upload — local backend only)
# ---------------------------------------------------------------------------


async def _handle_put(
    scope: dict[str, object],
    receive: ASGIReceive,
    send: ASGISend,
    *,
    settings: Settings,
    owner_name: str,
    repo_name: str,
    oid: str,
) -> None:
    """Serve `PUT /info/lfs/objects/{oid}` (local backend only).

    Auth: write-required (owner / admin).
    Body: streamed; sha256 + size verified by the store.
    On success: bumps the user's `UserUsage` and writes an `lfs.upload`
    audit row.
    """
    repo_pair = await _load_repo(owner_name=owner_name, repo_name=repo_name)
    if repo_pair is None:
        await _send_404(send, "repository not found")
        return
    repo_row, owner = repo_pair

    headers = scope.get("headers")
    auth_header = _header_value(headers, "authorization")
    user = await resolve_git_identity(auth_header, settings=settings)
    try:
        await authorize(user, repo=repo_row, owner=owner, action=GitAction.PUSH)
    except UnauthorizedError:
        await _send_401(send)
        return
    except ForbiddenError as exc:
        await _send_403(send, str(exc) or "forbidden")
        return

    # Content-Length gate — refuse to even read the body if it would
    # exceed the per-object cap, so a malicious client cannot tie up
    # bandwidth pushing bytes we'd reject anyway.
    cl = _header_value(headers, "content-length")
    if cl is not None:
        try:
            content_length = int(cl)
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > settings.lfs_max_object_bytes:
            await _send_status(
                send,
                status=413,
                payload={
                    "error": (
                        f"object exceeds per-object limit "
                        f"{settings.lfs_max_object_bytes}"
                    )
                },
            )
            return
        size_for_quota = content_length
    else:
        # No Content-Length: we'll enforce the same cap on the running
        # total. The store performs the definitive check on size +
        # sha256 at completion.
        size_for_quota = settings.lfs_max_object_bytes

    # Quota check before streaming, so we never tie up a multi-GB upload
    # for a user who could not afford it. PUSH auth already happened.
    factory = get_session_factory()
    async with factory() as session:
        owner_for_quota = (
            await session.execute(select(User).where(User.id == owner.id))
        ).scalar_one()
        try:
            from outo_models.repos.quota import check_push_allowed
            await check_push_allowed(session, owner_for_quota, size_for_quota)
            await session.commit()
        except QuotaExceededError as exc:
            await _send_status(send, status=413, payload={"error": str(exc)})
            return

    try:
        store = create_object_store(settings, base_url_override=_request_base_url(scope))
    except Exception as exc:
        await _send_status(send, status=500, payload={"error": str(exc)})
        return

    if not isinstance(store, LocalObjectStore):
        await _send_status(
            send,
            status=501,
            payload={
                "error": "this server's LFS backend does not support proxied uploads"
            },
        )
        return

    body_iter = _stream_request_body(receive, limit=settings.lfs_max_object_bytes)
    try:
        written = await store.write_object(
            oid, body_iter, expected_size=size_for_quota
        )
    except ValidationFailedError as exc:
        await _send_status(send, status=422, payload={"error": str(exc)})
        return
    except FileExistsError as exc:  # pragma: no cover - defensive
        await _send_status(send, status=409, payload={"error": str(exc)})
        return
    except ValueError:
        # Streaming cap hit.
        await _send_status(
            send, status=413, payload={"error": "object too large"}
        )
        return

    # Persist usage + audit log; commit on success.
    async with factory() as session:
        owner_row = (
            await session.execute(select(User).where(User.id == owner.id))
        ).scalar_one()
        await add_usage(session, owner_row, written)
        session.add(
            AuditLog(
                actor_id=user.id if user is not None else None,
                action="lfs.upload",
                target_type="repo",
                target_id=str(repo_row.id),
                detail=json.dumps(
                    {"oid": oid, "size": written, "owner": owner_name}
                ),
            )
        )
        await session.commit()

    await _send_status(send, status=200, payload={"ok": True})


# ---------------------------------------------------------------------------
# object GET (download)
# ---------------------------------------------------------------------------


async def _handle_get(
    scope: dict[str, object],
    receive: ASGIReceive,
    send: ASGISend,
    *,
    settings: Settings,
    owner_name: str,
    repo_name: str,
    oid: str,
) -> None:
    """Serve `GET /info/lfs/objects/{oid}` (local backend only).

    Auth: visibility-driven. Public repos are anonymous-readable;
    private repos require the owner or an admin. The S3 backend never
    reaches this handler.
    """
    repo_pair = await _load_repo(owner_name=owner_name, repo_name=repo_name)
    if repo_pair is None:
        await _send_404(send, "repository not found")
        return
    repo_row, owner = repo_pair

    headers = scope.get("headers")
    auth_header = _header_value(headers, "authorization")
    user = await resolve_git_identity(auth_header, settings=settings)
    try:
        await authorize(user, repo=repo_row, owner=owner, action=GitAction.PULL)
    except UnauthorizedError:
        await _send_401(send)
        return
    except ForbiddenError as exc:
        await _send_403(send, str(exc) or "forbidden")
        return

    try:
        store = create_object_store(settings, base_url_override=_request_base_url(scope))
    except Exception as exc:
        await _send_status(send, status=500, payload={"error": str(exc)})
        return

    if not isinstance(store, LocalObjectStore):
        await _send_status(
            send,
            status=501,
            payload={
                "error": "this server's LFS backend does not support proxied downloads"
            },
        )
        return

    if not await store.has_object(oid):
        await _send_404(send, "object not found")
        return

    size = await store.object_size(oid)
    headers_out: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/octet-stream"),
    ]
    if size is not None:
        headers_out.append(
            (b"content-length", str(size).encode("ascii"))
        )

    await send(
        {"type": "http.response.start", "status": 200, "headers": headers_out}
    )

    try:
        async for chunk in store.read_object(oid):
            if not chunk:
                continue
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                }
            )
    except FileNotFoundError:
        pass

    await send({"type": "http.response.body", "body": b"", "more_body": False})


# ---------------------------------------------------------------------------
# dispatcher (called by GitSmartService)
# ---------------------------------------------------------------------------


async def lfs_dispatch(
    scope: dict[str, object],
    receive: ASGIReceive,
    send: ASGISend,
    *,
    settings: Settings,
    owner_name: str,
    repo_name: str,
    rest: list[str],
) -> None:
    """Route the LFS sub-path to the right handler.

    `rest` is the path segment list AFTER the owner / repo prefix, so
    for `/{owner}/{repo}.git/info/lfs/objects/batch` it is
    `["info", "lfs", "objects", "batch"]`. Anything outside the four
    recognized endpoints returns 404.
    """
    method = str(scope.get("method", "GET")).upper()

    if method not in ("GET", "POST", "PUT"):
        await _send_405(send, "GET, POST, PUT")
        return

    sub = rest[2:] if len(rest) >= 2 and rest[:2] == ["info", "lfs"] else []
    if not sub:
        await _send_404(send, "not found")
        return

    head = sub[0]
    tail = sub[1:]

    if head == "locks":
        # Everything under `/info/lfs/locks*` is unimplemented — locks
        # are v2 work and the dispatcher does not need to know the
        # shape of the lock protocol to refuse it.
        await lfs_not_supported(scope, receive, send)
        return

    if head == "objects":
        if tail == ["batch"]:
            if method != "POST":
                await _send_405(send, "POST")
                return
            await _handle_batch(
                scope,
                receive,
                send,
                settings=settings,
                owner_name=owner_name,
                repo_name=repo_name,
            )
            return
        if len(tail) == 1:
            oid = tail[0]
            if method == "PUT":
                await _handle_put(
                    scope,
                    receive,
                    send,
                    settings=settings,
                    owner_name=owner_name,
                    repo_name=repo_name,
                    oid=oid,
                )
                return
            if method == "GET":
                await _handle_get(
                    scope,
                    receive,
                    send,
                    settings=settings,
                    owner_name=owner_name,
                    repo_name=repo_name,
                    oid=oid,
                )
                return
            await _send_405(send, "GET, PUT")
            return
        await _send_404(send, "not found")
        return

    # Anything else under /info/lfs/* that we don't recognize.
    await _send_404(send, "not found")


__all__ = ["LFS_CONTENT_TYPE", "lfs_dispatch", "lfs_not_supported"]