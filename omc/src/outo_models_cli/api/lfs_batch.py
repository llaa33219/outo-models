"""LFS batch request/response parsing.

The server's `/info/lfs/objects/batch` endpoint accepts an
`{"operation": "upload", "objects": [{"oid", "size"}]}` body and returns
a response with one entry per object. Each entry has either an
`actions.upload` block (with an absolute `href` the client streams the
raw bytes to) or a per-object `error` block (413 size cap, 413 quota,
404 unknown object for the download operation). An object that is
already on the server comes back WITHOUT `actions` AND without `error`
— the client interprets that as "skip the PUT, write pointer text".

This module is the pure wire-format layer: it sends the request and
parses the response into typed dataclasses that the upload command can
drain with one loop. The actual streaming PUT lives in `lfs_upload.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from outo_models_cli.api import send, unwrap
from outo_models_cli.api.lfs import LFS_CONTENT_TYPE
from outo_models_cli.errors import BadResponseError, map_response_error


@dataclass(frozen=True, slots=True)
class BatchAction:
    """Wire-level action handed back by the server for one object."""

    oid: str
    size: int
    href: str
    header: dict[str, str] = field(default_factory=dict)
    expires_in: int | None = None


@dataclass(frozen=True, slots=True)
class BatchObjectError:
    """Per-object failure surfaced by the LFS batch response."""

    oid: str
    size: int
    code: int
    message: str


def batch_upload(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    objects: list[dict[str, object]],
) -> tuple[list[BatchAction], list[BatchObjectError], list[str]]:
    """POST the LFS upload batch; split the response into actions / errors / present.

    Returns:
        * `actions` — objects the server wants uploaded, in input order.
        * `errors` — per-object failures (413 size cap, 413 quota, ...).
        * `present` — oids the server already has; the caller skips the PUT
          and still writes the pointer text for these at commit time.
    """
    body = {
        "operation": "upload",
        "transfers": ["basic"],
        "objects": objects,
    }
    response = send(
        client,
        "POST",
        f"/{owner}/{name}.git/info/lfs/objects/batch",
        headers={"Accept": LFS_CONTENT_TYPE, "Content-Type": LFS_CONTENT_TYPE},
        json=body,
    )
    if response.status_code == 404:
        raise BadResponseError(
            "Server does not support Git LFS at this endpoint. "
            "Upgrade the server or contact the operator.",
        )
    if response.status_code == 406:
        raise BadResponseError(
            "Server rejected the LFS request (missing Accept: application/vnd.git-lfs+json).",
        )
    if response.status_code == 415:
        raise BadResponseError(
            "Server rejected the LFS request (wrong Content-Type).",
        )
    if response.status_code >= 400:
        raise map_response_error(response)
    payload = unwrap(response)
    raw_objects = payload.get("objects")
    if not isinstance(raw_objects, list):
        raise BadResponseError("LFS batch response is missing `objects`.")

    actions: list[BatchAction] = []
    errors: list[BatchObjectError] = []
    present: list[str] = []

    for entry in raw_objects:
        if not isinstance(entry, dict):
            continue
        oid = str(entry.get("oid", ""))
        size_val = entry.get("size", 0)
        size = int(size_val) if isinstance(size_val, (int, float)) else 0
        error_raw = entry.get("error")
        if isinstance(error_raw, dict):
            code_raw = error_raw.get("code")
            code = int(code_raw) if isinstance(code_raw, (int, float)) else 0
            message = str(error_raw.get("message", ""))
            errors.append(BatchObjectError(oid=oid, size=size, code=code, message=message))
            continue
        actions_raw = entry.get("actions")
        if not isinstance(actions_raw, dict):
            # No actions AND no error → object is already stored server-side.
            present.append(oid)
            continue
        upload_raw = actions_raw.get("upload")
        if not isinstance(upload_raw, dict):
            errors.append(
                BatchObjectError(
                    oid=oid,
                    size=size,
                    code=500,
                    message="LFS response missing `actions.upload`.",
                )
            )
            continue
        href = str(upload_raw.get("href", ""))
        if not href:
            errors.append(
                BatchObjectError(
                    oid=oid,
                    size=size,
                    code=500,
                    message="LFS response missing upload `href`.",
                )
            )
            continue
        header_raw = upload_raw.get("header") or {}
        header: dict[str, str] = {}
        if isinstance(header_raw, dict):
            for k, v in header_raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    header[k] = v
        expires_in_raw = upload_raw.get("expires_in")
        expires_in: int | None = None
        if isinstance(expires_in_raw, (int, float)):
            expires_in = int(expires_in_raw)
        actions.append(
            BatchAction(
                oid=oid,
                size=size,
                href=href,
                header=header,
                expires_in=expires_in,
            )
        )
    return actions, errors, present


__all__ = ["BatchAction", "BatchObjectError", "batch_upload"]
