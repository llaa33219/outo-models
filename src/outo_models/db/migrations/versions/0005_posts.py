"""global Posts + emoji reactions + flat comments

Revision ID: 0005_posts
Revises: 0004_comment_replies
Create Date: 2026-09-27 00:00:00

Adds the site-wide Posts feature:

    posts              — site-wide posts (short or long), with optional
                         repo link, CHECK on `kind`, and the
                         title/kind consistency constraint that locks
                         short → no-title and long → title-required.
    post_reactions     — per-user emoji reactions from a fixed 8-glyph
                         palette (CHECK), UNIQUE(post_id, user_id, emoji)
                         so a duplicate POST collapses to a toggle.
    post_comments      — flat comments on posts (no replies in v0.5;
                         the per-repo community tab keeps the reply chain).
                         Cascades on post delete so reactions + comments
                         go away with the post row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_posts"
down_revision: str | Sequence[str] | None = "0004_comment_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REACTION_PALETTE = (
    "\U0001f44d",
    "\U0001f44e",
    "\u2764\ufe0f",
    "\U0001f680",
    "\U0001f389",
    "\U0001f62e",
    "\U0001f615",
    "\U0001f440",
)
_PALETTE_CSV = ",".join(f"'{glyph}'" for glyph in _REACTION_PALETTE)


def upgrade() -> None:
    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_posts"),
        sa.CheckConstraint(
            "kind IN ('short', 'long')",
            name="ck_posts_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'long' AND title IS NOT NULL AND length(title) > 0) OR "
            "(kind = 'short' AND title IS NULL)",
            name="ck_posts_title_kind_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_posts_author_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repos.id"],
            name="fk_posts_repo_id_repos",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_posts_author_id", "posts", ["author_id"])
    op.create_index("ix_posts_repo_id", "posts", ["repo_id"])
    op.create_index("ix_posts_created_at", "posts", ["created_at"])

    op.create_table(
        "post_reactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("emoji", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_post_reactions"),
        sa.UniqueConstraint(
            "post_id",
            "user_id",
            "emoji",
            name="uq_post_reactions_post_id_user_id_emoji",
        ),
        sa.CheckConstraint(
            f"emoji IN ({_PALETTE_CSV})",
            name="ck_post_reactions_emoji",
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name="fk_post_reactions_post_id_posts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_post_reactions_user_id_users",
        ),
    )
    op.create_index("ix_post_reactions_post_id", "post_reactions", ["post_id"])
    op.create_index("ix_post_reactions_post_id_emoji", "post_reactions", ["post_id", "emoji"])

    op.create_table(
        "post_comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_post_comments"),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name="fk_post_comments_post_id_posts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_post_comments_author_id_users",
        ),
    )
    op.create_index("ix_post_comments_post_id", "post_comments", ["post_id"])
    op.create_index("ix_post_comments_author_id", "post_comments", ["author_id"])


def downgrade() -> None:
    op.drop_index("ix_post_comments_author_id", table_name="post_comments")
    op.drop_index("ix_post_comments_post_id", table_name="post_comments")
    op.drop_table("post_comments")

    op.drop_index("ix_post_reactions_post_id_emoji", table_name="post_reactions")
    op.drop_index("ix_post_reactions_post_id", table_name="post_reactions")
    op.drop_table("post_reactions")

    op.drop_index("ix_posts_created_at", table_name="posts")
    op.drop_index("ix_posts_repo_id", table_name="posts")
    op.drop_index("ix_posts_author_id", table_name="posts")
    op.drop_table("posts")
