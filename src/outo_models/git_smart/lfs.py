"""LFS stub handler.

Git LFS is a v1 stub: every `*.git/info/lfs/*` request returns
`501 Not Implemented` with a stable JSON envelope pointing operators at
the documentation URL. WP-13 routes the request here; this module is the
single seam a real implementation will replace later.

The body is rendered as plain JSON bytes rather than via a third-party
encoder so the wire format survives any future swap to a structured
encoder: the only contract the client depends on is the JSON shape and
the 501 status.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

#: Stable documentation URL embedded in the 501 body. WP-13 routers should
#: link to this page from the repo-detail UI; changing it is a wire-level
#: breaking change for every LFS-aware client.
_DOCS_PATH = "/docs/git-repos"

#: Pre-rendered JSON body for the 501 response. Computed once at import
#: time so each request pays zero allocation cost.
_BODY_BYTES: bytes = json.dumps(
    {"error": "Git LFS is not supported yet", "docs": _DOCS_PATH}
).encode("utf-8")

#: ASGI `send` callable — the parameter signature is `object`-typed in the
#: spec to keep call sites untyped, but every conforming ASGI server
#: passes a coroutine.
ASGISend = Callable[[dict[str, object]], Awaitable[None]]


async def lfs_not_supported(
    scope: object,
    receive: Callable[[], Awaitable[dict[str, object]]],
    send: ASGISend,
) -> None:
    """ASGI handler that always responds `501` with the LFS stub body.

    The `scope` and `receive` parameters are unused: LFS clients learn
    "not implemented" from the status code alone and stop retrying.
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


__all__ = ["lfs_not_supported"]
