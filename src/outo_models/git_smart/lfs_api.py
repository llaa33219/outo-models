"""Git LFS batch API: request/response shapes + per-object decision logic.

This module is the *pure* layer of the LFS implementation: it knows the
wire format and what each object should resolve to (an action or an
error), but it does not own HTTP, auth, or the store. The handler in
`outo_models.git_smart.lfs` wires the ASGI surface to these helpers.

Wire contract reference:
    https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md

A request looks like:

    {
      "operation": "upload" | "download",
      "transfers": ["basic"],
      "objects": [{ "oid": "<sha256>", "size": <int> }, ...]
    }

A response is:

    {
      "transfer": "basic",
      "objects": [
        {
          "oid": "...", "size": ...,
          "actions": {
            "upload":   {"href": ..., "header": {...}, "expires_in": ...}
          }
        },
        {
          "oid": "...", "size": ...,
          "error": {"code": ..., "message": ...}
        }
      ]
    }

Per the spec, errors are per-object, NOT per-batch: a single bad object
must not fail the whole request. The handlers must therefore accumulate
object entries one-by-one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.config import Settings
from outo_models.exceptions import QuotaExceededError, ValidationFailedError
from outo_models.objectstore.base import LfsAction, ObjectStore
from outo_models.repos.quota import check_push_allowed

# ---------------------------------------------------------------------------
# request / response value objects
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BatchObjectRequest:
    """One object entry inside a batch request."""

    oid: str
    size: int


@dataclass(slots=True)
class BatchRequest:
    """A parsed batch request payload.

    `transfers` is the list of transfer modes the client understands;
    we always answer with `basic`. `objects` may legitimately be empty
    — we still answer 200 with `objects: []` so the client does not
    retry forever.
    """

    operation: str
    objects: list[BatchObjectRequest] = field(default_factory=list)
    transfers: list[str] = field(default_factory=lambda: ["basic"])


@dataclass(slots=True)
class BatchObjectResponse:
    """One object entry in the batch response.

    Exactly one of `actions` / `error` is populated per the LFS spec:
    a successful entry carries action URLs, a failed entry carries the
    `{code, message}` envelope and the action URLs are omitted.
    """

    oid: str
    size: int
    actions: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(slots=True)
class BatchResponse:
    """The top-level batch response envelope."""

    objects: list[BatchObjectResponse] = field(default_factory=list)

    @property
    def transfer(self) -> str:
        return "basic"

    def to_wire(self) -> dict[str, Any]:
        return {
            "transfer": self.transfer,
            "objects": [_object_to_wire(o) for o in self.objects],
        }


def _object_to_wire(o: BatchObjectResponse) -> dict[str, Any]:
    out: dict[str, Any] = {"oid": o.oid, "size": o.size}
    if o.actions is not None:
        out["actions"] = o.actions
    if o.error is not None:
        out["error"] = o.error
    return out


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse_batch_body(body: bytes) -> BatchRequest:
    """Parse the JSON body of a `/info/lfs/objects/batch` request.

    Raises `ValidationFailedError` (HTTP 422) on:
        - invalid JSON,
        - missing `operation` / `objects`,
        - an `operation` other than `"upload"` / `"download"`,
        - malformed object entries,
        - `transfers` claiming an unsupported mode.

    The error message is in English so git-lfs logs stay readable; the
    Korean docs page explains the protocol to operators.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationFailedError("batch body must be a JSON object")

    operation = payload.get("operation")
    if operation not in ("upload", "download"):
        raise ValidationFailedError(
            f"operation must be 'upload' or 'download', got {operation!r}"
        )

    transfers_raw = payload.get("transfers", ["basic"])
    if not isinstance(transfers_raw, list) or not transfers_raw:
        transfers = ["basic"]
    elif not all(isinstance(t, str) for t in transfers_raw):
        raise ValidationFailedError("transfers must be a list of strings")
    else:
        transfers = transfers_raw
    if "basic" not in transfers:
        # We only support the `basic` transfer mode.
        raise ValidationFailedError(
            "server only supports the 'basic' transfer mode"
        )

    raw_objects = payload.get("objects")
    if not isinstance(raw_objects, list):
        raise ValidationFailedError("objects must be a list")

    parsed_objects: list[BatchObjectRequest] = []
    for entry in raw_objects:
        if not isinstance(entry, dict):
            raise ValidationFailedError("each object entry must be a dict")
        oid = entry.get("oid")
        size = entry.get("size")
        if not isinstance(oid, str) or len(oid) != 64:
            raise ValidationFailedError(f"object oid missing or malformed: {entry!r}")
        if not isinstance(size, int) or size < 0:
            raise ValidationFailedError(f"object size missing or negative: {entry!r}")
        parsed_objects.append(BatchObjectRequest(oid=oid, size=size))

    return BatchRequest(
        operation=operation,
        objects=parsed_objects,
        transfers=transfers,
    )


# ---------------------------------------------------------------------------
# object decisions
# ---------------------------------------------------------------------------


def _action_payload(action: LfsAction) -> dict[str, Any]:
    """Translate an `LfsAction` to the wire-format dict.

    The spec field name is singular `header` (NOT `headers`), even when
    more than one header is sent. Empty dicts are omitted so the wire
    shape stays clean for the local backend.
    """
    payload: dict[str, Any] = {"href": action.href, "expires_in": action.expires_in}
    if action.headers:
        payload["header"] = dict(action.headers)
    return payload


async def _build_upload_entry(
    *,
    store: ObjectStore,
    owner: str,
    repo: str,
    oid: str,
    size: int,
    session: AsyncSession,
    user_id: int,
    settings: Settings,
) -> BatchObjectResponse:
    """Resolve one object in an upload batch.

    Decision tree (per LFS spec):
        1. Object already in store → entry WITHOUT `actions`
           (client skips the upload).
        2. Object larger than `lfs_max_object_bytes` → per-object error
           with `code=413`.
        3. Object would push the user over quota → per-object error
           with `code=413` (do NOT fail the whole batch).
        4. Otherwise → `actions.upload` from the store.
    """
    if await store.has_object(oid):
        return BatchObjectResponse(oid=oid, size=size)

    if size > settings.lfs_max_object_bytes:
        return BatchObjectResponse(
            oid=oid,
            size=size,
            error={
                "code": 413,
                "message": (
                    f"object size {size} exceeds per-object limit "
                    f"{settings.lfs_max_object_bytes}"
                ),
            },
        )

    # Quota check needs the live User row, so we re-fetch by id.
    from outo_models.db import User

    user_row = await session.get(User, user_id)
    if user_row is None:  # pragma: no cover - defensive
        return BatchObjectResponse(
            oid=oid,
            size=size,
            error={"code": 401, "message": "authenticated user no longer exists"},
        )

    try:
        await check_push_allowed(session, user_row, size)
        await session.commit()
    except QuotaExceededError as exc:
        await session.rollback()
        return BatchObjectResponse(
            oid=oid,
            size=size,
            error={"code": 413, "message": str(exc)},
        )

    action = await store.make_upload_action(
        owner=owner, repo=repo, oid=oid, size=size
    )
    return BatchObjectResponse(
        oid=oid,
        size=size,
        actions={"upload": _action_payload(action)},
    )


async def _build_download_entry(
    *,
    store: ObjectStore,
    owner: str,
    repo: str,
    oid: str,
    size: int,
) -> BatchObjectResponse:
    """Resolve one object in a download batch.

    Decision tree:
        1. Object missing → per-object error with `code=404`.
        2. Otherwise → `actions.download` from the store.
    """
    if not await store.has_object(oid):
        return BatchObjectResponse(
            oid=oid,
            size=size,
            error={"code": 404, "message": "object not found"},
        )
    action = await store.make_download_action(
        owner=owner, repo=repo, oid=oid, size=size
    )
    return BatchObjectResponse(
        oid=oid,
        size=size,
        actions={"download": _action_payload(action)},
    )


async def handle_batch(
    *,
    request: BatchRequest,
    store: ObjectStore,
    owner_name: str,
    repo_name: str,
    actor_id: int | None,
    session: AsyncSession,
    settings: Settings,
) -> BatchResponse:
    """Build the full batch response for `request`.

    `actor_id` is the authenticated user's PK (None for anonymous
    download of a public repo). For uploads it MUST be set; the handler
    in `lfs.py` is responsible for rejecting anonymous uploads upstream.
    """
    entries: list[BatchObjectResponse] = []
    for obj in request.objects:
        if request.operation == "upload":
            assert actor_id is not None  # handler enforces auth
            entry = await _build_upload_entry(
                store=store,
                owner=owner_name,
                repo=repo_name,
                oid=obj.oid,
                size=obj.size,
                session=session,
                user_id=actor_id,
                settings=settings,
            )
        else:
            entry = await _build_download_entry(
                store=store,
                owner=owner_name,
                repo=repo_name,
                oid=obj.oid,
                size=obj.size,
            )
        entries.append(entry)
    return BatchResponse(objects=entries)


def dedup_objects(objects: Iterable[BatchObjectRequest]) -> list[BatchObjectRequest]:
    """Collapse duplicate `(oid, size)` pairs.

    git-lfs occasionally retries with the same object set; the spec is
    forgiving about that, but a duplicate would produce a duplicate
    response entry and trigger two PUTs. First occurrence wins.
    """
    seen: set[tuple[str, int]] = set()
    out: list[BatchObjectRequest] = []
    for o in objects:
        key = (o.oid, o.size)
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


__all__ = [
    "BatchObjectRequest",
    "BatchObjectResponse",
    "BatchRequest",
    "BatchResponse",
    "dedup_objects",
    "handle_batch",
    "parse_batch_body",
]