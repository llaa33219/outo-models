"""Repo ORM model.

A repo is a single git repository (model / dataset / space) owned by a user.
`path` is a relative path under `data_dir / repos` (see `utils.paths.repo_path`)
so that the on-disk layout has exactly one home per row.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outo_models.db.models.base import Base, IntIdMixin, TimestampWithUpdateMixin
from outo_models.db.models.user import User


class Repo(IntIdMixin, TimestampWithUpdateMixin, Base):
    """A user-owned git repository.

    `kind` discriminates model / dataset / space so that the same owner can
    legitimately reuse a name across kinds. The `(owner_id, kind, name)`
    unique constraint prevents accidental duplicates within a kind.
    """

    __tablename__ = "repos"

    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_repos_owner_id_users"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", server_default="private"
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_branch: Mapped[str] = mapped_column(
        String(64), nullable=False, default="main", server_default="main"
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    downloads_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "name", name="uq_repos_owner_id_kind_name"),
        Index("ix_repos_owner_id", "owner_id"),
        Index("ix_repos_kind", "kind"),
    )

    owner: Mapped[User] = relationship("User", lazy="raise")


__all__ = ["Repo"]
