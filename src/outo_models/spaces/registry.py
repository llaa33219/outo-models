"""Spaces registry — the v1 metadata layer.

Spaces share the same on-disk + DB infrastructure as models and datasets
(they are just `Repo(kind="space")` rows that go through
`repos.create.create_repo` and `repos.delete.delete_repo`). What they
add on top is the SDK choice: in v1 the SDK has no schema column, so it
lives as a tiny JSON sidecar under `spaces_dir/<owner>/<name>.json`. The
filesystem is the source of truth for SDK; the DB row is the source of
truth for everything else. A missing sidecar file is not an error — it
means "default SDK = static", which is what every space used to have
before the JSON file was introduced.

Routers own transactions: every function in this module takes an
`AsyncSession` and never calls `.commit()` / `.rollback()`. Side effects
on disk are placed AFTER the DB flush so a unique-constraint or FK
failure surfaces before we leave orphan files behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from outo_models.db import Repo, User
from outo_models.exceptions import NotFoundError, ValidationFailedError
from outo_models.repos.create import create_repo
from outo_models.repos.delete import delete_repo
from outo_models.repos.models import RepoKind, Visibility
from outo_models.utils.paths import spaces_dir
from outo_models.utils.time import utcnow

# SDKs the v1 metadata layer accepts. Order is the order they are
# advertised in docs / dropdowns; membership is what `create_space`
# checks against. "static" is the implicit default for every space that
# predates the JSON sidecar — see `DEFAULT_SDK`.
SUPPORTED_SDKS: tuple[str, ...] = ("static", "gradio", "streamlit", "docker")
DEFAULT_SDK = "static"


@dataclass(frozen=True, slots=True)
class SpaceMeta:
    """On-disk sidecar for one Space.

    `sdk` is the only field that influences behavior; `updated_at` is
    bookkeeping so a future UI can show "last edited" without parsing the
    DB row. `updated_at` defaults to "now" so callers constructing a
    fresh `SpaceMeta` (e.g. for `write_space_meta`) do not have to thread
    a clock through. `frozen=True` keeps the value immutable once read.
    """

    sdk: str
    updated_at: datetime = field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------


def _space_meta_path(owner: str, name: str) -> Path:
    """Return the absolute path of `<owner>/<name>.json` under `spaces_dir()`.

    `spaces_dir()` already points at `<data_dir>/spaces`; the owner
    segment is a flat directory so a single `iterdir()` lists every
    space an owner has registered.
    """
    return spaces_dir() / owner / f"{name}.json"


def write_space_meta(owner: str, name: str, meta: SpaceMeta) -> Path:
    """Write `meta` to `<spaces_dir>/<owner>/<name>.json`.

    Creates the owner segment if missing. Atomic at the `write_text` level
    (a partial file would only happen on a kernel crash mid-write, and
    the next `read_space_meta` falls back to the default SDK in that
    case). Returns the on-disk path so callers can re-read it for
    confirmation-free assertion-free checks.
    """
    path = _space_meta_path(owner, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sdk": meta.sdk,
        "updated_at": meta.updated_at.isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def read_space_meta(owner: str, name: str) -> SpaceMeta:
    """Return the on-disk `SpaceMeta`, or `DEFAULT_SDK` if the file is absent.

    A missing sidecar is not an error — it means "this space predates the
    JSON file, or it was created by a path that didn't write one". The
    fallback is the same SDK every space had before the sidecar was
    introduced, so the caller sees a coherent view either way.
    """
    path = _space_meta_path(owner, name)
    if not path.is_file():
        return SpaceMeta(sdk=DEFAULT_SDK, updated_at=utcnow())
    payload = json.loads(path.read_text())
    return SpaceMeta(
        sdk=str(payload.get("sdk", DEFAULT_SDK)),
        updated_at=datetime.fromisoformat(str(payload["updated_at"]))
        if "updated_at" in payload
        else utcnow(),
    )


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------


def _validate_sdk(sdk: str) -> str:
    """Raise `ValidationFailedError` if `sdk` is not in `SUPPORTED_SDKS`.

    Returned unchanged on success so callers can write the result straight
    into the sidecar without re-typing the parameter.
    """
    if sdk not in SUPPORTED_SDKS:
        raise ValidationFailedError(
            f"unsupported space sdk: {sdk!r} "
            f"(supported: {', '.join(SUPPORTED_SDKS)})"
        )
    return sdk


async def create_space(
    session: AsyncSession,
    *,
    owner: User,
    name: str,
    sdk: str = DEFAULT_SDK,
    visibility: Visibility = Visibility.PRIVATE,
    description: str | None = None,
) -> Repo:
    """Create a Space and its on-disk sidecar.

    Steps (in order):
        1. Validate `sdk` against `SUPPORTED_SDKS` — runs BEFORE any
           disk or DB work, so an invalid SDK never produces a half-built
           space.
        2. Delegate to `repos.create.create_repo` with
           `kind=RepoKind.SPACE`. That call handles the bare repo on
           disk, the `Repo` row, quota rows, and the `repo.create` audit
           entry, and returns a flushed-but-not-committed `Repo`.
        3. Write `<spaces_dir>/<owner>/<name>.json` with the chosen SDK.
           Sidecar write is the last step so a `ConflictError` from step
           2 never leaves an orphan JSON file behind.

    The session is NOT committed — routers own the transaction.
    """
    sdk = _validate_sdk(sdk)

    repo = await create_repo(
        session,
        owner=owner,
        name=name,
        kind=RepoKind.SPACE,
        visibility=visibility,
        description=description,
    )

    # On-disk sidecar last so the DB-side rollback path (a flush error
    # after create_repo) cannot leave an orphan JSON file pointing at
    # a repo that does not exist.
    write_space_meta(
        owner.username, name, SpaceMeta(sdk=sdk, updated_at=utcnow())
    )

    return repo


async def get_space(
    session: AsyncSession, *, owner_name: str, name: str
) -> Repo:
    """Return the Space row for `<owner_name>/<name>` or raise `NotFoundError`.

    Eager-loads `owner` so routers can render `owner.username` without a
    follow-up query. A row whose `kind` is not `"space"` is rejected with
    the same `NotFoundError` — kind discrimination is a safety property,
    not a discoverability nicety.
    """
    stmt = (
        select(Repo)
        .where(
            Repo.name == name,
            Repo.kind == RepoKind.SPACE.value,
        )
        .options(selectinload(Repo.owner))
    )
    repo = (
        await session.execute(
            stmt.join(Repo.owner).where(User.username == owner_name)
        )
    ).scalar_one_or_none()
    if repo is None:
        raise NotFoundError(
            f"space not found: {owner_name}/{name}"
        )
    return repo


async def list_spaces(
    session: AsyncSession,
    *,
    owner_name: str | None = None,
    include_private: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Repo]:
    """List Spaces with visibility + owner filtering + offset pagination.

    Default visibility filter is `PUBLIC` only, mirroring the Hugging Face
    convention: a `GET /spaces` response is the public catalog.
    `include_private=True` widens it for the owner's own dashboard; it
    MUST NOT be reachable by a non-owner caller, which is the router's
    responsibility.
    """
    stmt = select(Repo).where(Repo.kind == RepoKind.SPACE.value)
    if not include_private:
        stmt = stmt.where(Repo.visibility == Visibility.PUBLIC.value)
    if owner_name is not None:
        stmt = stmt.join(Repo.owner).where(User.username == owner_name)
    stmt = stmt.order_by(Repo.id).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def update_space(
    session: AsyncSession,
    *,
    space: Repo,
    visibility: Visibility | None = None,
    description: str | None = None,
) -> Repo:
    """Apply visibility / description changes to an existing Space row.

    The SDK is intentionally not updatable from this API in v1 — the
    choice is a property of the Space (what its `app.py` / `Dockerfile`
    actually is), not an arbitrary metadata toggle, and changing it
    silently would let a public Space lie about its runtime.

    `space` may arrive detached (routers commonly read it in an earlier
    request and pass it through). `session.merge` re-attaches it so the
    subsequent attribute writes produce an UPDATE on flush.

    The session is NOT committed — routers own the transaction.
    """
    space = await session.merge(space)
    if visibility is not None:
        space.visibility = visibility.value
    if description is not None:
        space.description = description
    await session.flush()
    return space


async def delete_space(session: AsyncSession, *, owner: User, name: str) -> None:
    """Delete a Space row, its bare repo, and its sidecar file.

    Delegates the row + bare-repo work to `repos.delete.delete_repo`, then
    removes `<spaces_dir>/<owner>/<name>.json`. The sidecar removal is
    best-effort (`ignore_errors=True` semantics via `unlink(missing_ok)`)
    so a half-deleted install still cleans up cleanly.

    The session is NOT committed — routers own the transaction.
    """
    await delete_repo(session, owner=owner, name=name, kind=RepoKind.SPACE)
    # On-disk sidecar removal runs AFTER the delegated delete succeeds, so
    # a missing-repo `NotFoundError` from `delete_repo` never costs the
    # sidecar. `missing_ok=True` keeps the call idempotent against a
    # previously-deleted file.
    _space_meta_path(owner.username, name).unlink(missing_ok=True)


__all__ = [
    "DEFAULT_SDK",
    "SUPPORTED_SDKS",
    "SpaceMeta",
    "create_space",
    "delete_space",
    "get_space",
    "list_spaces",
    "read_space_meta",
    "update_space",
    "write_space_meta",
]