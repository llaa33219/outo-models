"""Revision ORM model.

A revision is one immutable git commit inside a repo. It exists so the audit /
listing paths can answer "what commits are in this repo?" without scanning the
bare git repo on disk.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin


class Revision(IntIdMixin, TimestampMixin, Base):
    """An immutable commit recorded against a repo.

    `commit_sha` is the full 40-character hex SHA-1 of the commit. `branch`
    records the branch the commit was pushed to; the same SHA can appear on
    multiple branches so uniqueness is intentionally not enforced.
    """

    __tablename__ = "revisions"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", name="fk_revisions_repo_id_repos"),
        nullable=False,
        index=True,
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    branch: Mapped[str] = mapped_column(
        String(64), nullable=False, default="main", server_default="main"
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_revisions_author_id_users"),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_revisions_repo_id", "repo_id"),
        Index("ix_revisions_commit_sha", "commit_sha"),
    )


__all__ = ["Revision"]
