"""comment replies (self-referencing FK)

Revision ID: 0004_comment_replies
Revises: 0003_profile_repo_color
Create Date: 2026-09-10 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_comment_replies"
down_revision: str | Sequence[str] | None = "0003_profile_repo_color"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("repo_comments") as batch_op:
        batch_op.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_repo_comments_parent_id_repo_comments",
            "repo_comments",
            ["parent_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("repo_comments") as batch_op:
        batch_op.drop_constraint("fk_repo_comments_parent_id_repo_comments", type_="foreignkey")
        batch_op.drop_column("parent_id")
