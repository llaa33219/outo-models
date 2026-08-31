"""Domain models for the repository layer.

Pure value objects shared by `create`, `delete`, `quota`, and `reflog`. These
types are deliberately free of SQLAlchemy and `dulwich` imports so the module
can be imported from anywhere (CLI, routers, tests) without dragging the rest
of the stack in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class RepoKind(StrEnum):
    """The kind of repository a user can own.

    `kind` discriminates model / dataset / space so the same owner can
    legitimately reuse a name across kinds. The `StrEnum` base gives the
    values string equality (which the DB column relies on) plus exhaustiveness
    from the standard library `Enum` machinery.
    """

    MODEL = "model"
    DATASET = "dataset"
    SPACE = "space"


class Visibility(StrEnum):
    """Whether a repository is publicly visible or owner-only.

    Default is `PRIVATE`; promotion to `PUBLIC` is an explicit choice the
    owner makes once the repo is ready to share.
    """

    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """Read-only view of a repository.

    Returned by higher-level helpers (e.g. reflog) that already have all the
    data they need without round-tripping to the database. `path` is the
    absolute on-disk location of the bare repository.
    """

    id: int
    owner: str
    name: str
    kind: RepoKind
    visibility: Visibility
    description: str | None
    default_branch: str
    size_bytes: int
    path: Path
    created_at: datetime


__all__ = ["RepoInfo", "RepoKind", "Visibility"]
