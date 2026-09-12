"""user profile fields + repo accent color

Revision ID: 0003_profile_repo_color
Revises: 0002_social
Create Date: 2026-09-10 00:00:00

Adds profile-surface columns to `users` (bio, interests, links — all
JSON/text, nullable) and an optional accent `color` (#RRGGBB) to `repos`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_profile_repo_color"
down_revision: str | Sequence[str] | None = "0002_social"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("bio", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("interests", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("links", sa.Text(), nullable=True))

    with op.batch_alter_table("repos") as batch_op:
        batch_op.add_column(sa.Column("color", sa.String(length=7), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repos") as batch_op:
        batch_op.drop_column("color")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("links")
        batch_op.drop_column("interests")
        batch_op.drop_column("bio")
