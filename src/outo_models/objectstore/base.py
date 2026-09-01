"""ObjectStore protocol — the seam WP-20's S3 backend implements against.

WP-20 owns the S3 implementation; this module only declares the protocol
the rest of the system speaks. The pinned `LfsAction` shape is what every
backend hands back from `make_upload_action` / `make_download_action`, and
`ObjectStore` is the structural interface the batch handler dispatches
through.

Local and S3 backends are constructed by `outo_models.objectstore.factory`;
this module deliberately knows nothing about paths, settings, or auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol


@dataclass(frozen=True, slots=True)
class LfsAction:
    """Wire-level action handed back to the git-lfs client.

    `href` is the URL the client PUTs / GETs the object bytes against.
    For the local backend this is the same-origin `/info/lfs/objects/{oid}`
    URL — git-lfs reuses the Basic credentials from the originating clone,
    so we ship no `Authorization` header. For the S3 backend, `href` points
    at the S3-compatible endpoint and `headers` carries the
    `x-amz-…` signing fields.

    `expires_in` is the number of seconds the URL is valid for; the local
    backend reuses `Settings.s3_presign_ttl_seconds` as a generic TTL even
    though the URL never actually expires.
    """

    href: str
    headers: dict[str, str]
    expires_in: int


class ObjectStore(Protocol):
    """Pluggable LFS object storage.

    Backends must be safe to share across requests: every method is async
    and the implementation is responsible for any concurrency control over
    its own on-disk / over-the-w3 primitives. `name` is a short tag the
    audit log records so operators can tell at a glance which backend
    served an upload.
    """

    name: ClassVar[str]

    async def make_upload_action(
        self,
        *,
        owner: str,
        repo: str,
        oid: str,
        size: int,
    ) -> LfsAction:
        """Return the URL + headers the client PUTs object bytes to.

        `oid` and `size` are passed through so the S3 backend can embed
        them in the signed headers; the local backend ignores them.
        """
        ...

    async def make_download_action(
        self,
        *,
        owner: str,
        repo: str,
        oid: str,
        size: int,
    ) -> LfsAction:
        """Return the URL + headers the client GETs object bytes from."""
        ...

    async def has_object(self, oid: str) -> bool:
        """Return True iff an object for `oid` is currently stored.

        Implementations must never follow symlinks: a malicious symlink
        planted under the local backend root must not register as a hit.
        """
        ...

    async def object_size(self, oid: str) -> int | None:
        """Return the stored size for `oid`, or `None` if missing.

        Symlinks are treated as missing — the returned `None` is the same
        shape callers see for a true absence.
        """
        ...

    async def delete_object(self, oid: str) -> None:
        """Remove the object for `oid`; idempotent on missing entries."""
        ...


__all__ = ["LfsAction", "ObjectStore"]
