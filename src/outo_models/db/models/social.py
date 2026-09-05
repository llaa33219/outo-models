"""Social-graph ORM models (likes, follows, comments).

Three small tables share the same shape: a pair of FKs to `users` /
`repos`, an optional body for comments, and a `created_at` timestamp.
Uniqueness is enforced at the DB layer so two concurrent POSTs to
`/like` collapse to one row instead of an `IntegrityError`.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin
from outo_models.db.models.user import User

_COMMENT_BODY_MAX = 4000


class RepoLike(IntIdMixin, TimestampMixin, Base):
    """A single user's like on a single repo.

    Uniqueness is `(user_id, repo_id)` so the same user cannot like the
    same repo twice; `id` exists only because every other table in the
    project inherits the integer-PK mixin.
    """

    __tablename__ = "repo_likes"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_repo_likes_user_id_users"),
        nullable=False,
    )
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", name="fk_repo_likes_repo_id_repos"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "repo_id", name="uq_repo_likes_user_id_repo_id"),
        Index("ix_repo_likes_repo_id", "repo_id"),
    )


class UserFollow(IntIdMixin, TimestampMixin, Base):
    """A directed `follower → followee` edge.

    The CHECK constraint keeps the row from referencing the same user on
    both sides — the service layer also enforces this so it can raise a
    typed `ForbiddenError` at the API boundary, but the DB-side guard
    protects direct writes that bypass the service.
    """

    __tablename__ = "user_follows"

    follower_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_follows_follower_id_users"),
        nullable=False,
    )
    followee_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_follows_followee_id_users"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("follower_id", "followee_id", name="uq_user_follows_follower_followee"),
        CheckConstraint("follower_id <> followee_id", name="ck_user_follows_not_self"),
        Index("ix_user_follows_followee_id", "followee_id"),
    )


class RepoComment(IntIdMixin, TimestampMixin, Base):
    """A user-authored comment on a repo.

    `body` is stored as `Text` because the API caps length at 4000 chars
    (the column would accept longer blobs but the API rejects them). The
    composite index on `(repo_id, created_at)` matches the listing
    query — newest-first pagination reads backwards through this index.
    """

    __tablename__ = "repo_comments"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", name="fk_repo_comments_repo_id_repos"),
        nullable=False,
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_repo_comments_author_id_users"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    author: Mapped[User] = relationship("User", lazy="raise")

    __table_args__ = (Index("ix_repo_comments_repo_id_created_at", "repo_id", "created_at"),)


__all__ = ["RepoComment", "RepoLike", "UserFollow"]
