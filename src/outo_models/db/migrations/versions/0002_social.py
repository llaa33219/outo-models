"""social graph + clone counter

Revision ID: 0002_social
Revises: 0001_initial
Create Date: 2026-09-05 00:00:00

Adds:

    repo_likes         — unique (user_id, repo_id) likes
    user_follows       — directed (follower_id, followee_id) edges with
                         a CHECK follower_id <> followee_id guard
    repo_comments      — free-text user comments; (repo_id, created_at)
                         index for newest-first listing

And ALTERs the existing `repos` table with a `downloads_count` column
    that defaults to 0 and is bumped by the git smart-HTTP PULL path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_social"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repo_likes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_repo_likes"),
        sa.UniqueConstraint("user_id", "repo_id", name="uq_repo_likes_user_id_repo_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_repo_likes_user_id_users"),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], name="fk_repo_likes_repo_id_repos"),
    )
    op.create_index("ix_repo_likes_repo_id", "repo_likes", ["repo_id"])

    op.create_table(
        "user_follows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("follower_id", sa.Integer(), nullable=False),
        sa.Column("followee_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_follows"),
        sa.UniqueConstraint("follower_id", "followee_id", name="uq_user_follows_follower_followee"),
        sa.CheckConstraint("follower_id <> followee_id", name="ck_user_follows_not_self"),
        sa.ForeignKeyConstraint(
            ["follower_id"], ["users.id"], name="fk_user_follows_follower_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["followee_id"], ["users.id"], name="fk_user_follows_followee_id_users"
        ),
    )
    op.create_index("ix_user_follows_followee_id", "user_follows", ["followee_id"])

    op.create_table(
        "repo_comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_repo_comments"),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], name="fk_repo_comments_repo_id_repos"),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_repo_comments_author_id_users"
        ),
    )
    op.create_index(
        "ix_repo_comments_repo_id_created_at",
        "repo_comments",
        ["repo_id", "created_at"],
    )

    # Clone counter. SQLite handles `ALTER TABLE ... ADD COLUMN` natively;
    # the batch-alter mode in env.py makes this safe on sqlite, and on
    # postgres it is the standard form.
    with op.batch_alter_table("repos") as batch_op:
        batch_op.add_column(
            sa.Column(
                "downloads_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("repos") as batch_op:
        batch_op.drop_column("downloads_count")

    op.drop_index("ix_repo_comments_repo_id_created_at", table_name="repo_comments")
    op.drop_table("repo_comments")
    op.drop_index("ix_user_follows_followee_id", table_name="user_follows")
    op.drop_table("user_follows")
    op.drop_index("ix_repo_likes_repo_id", table_name="repo_likes")
    op.drop_table("repo_likes")
