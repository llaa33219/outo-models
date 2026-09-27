"""post_reactions palette 8 -> 26 glyphs

Rebuilds the `ck_post_reactions_emoji` CHECK constraint so the emoji
column accepts the 18 glyphs added to `REACTION_PALETTE` in v0.6.1.
SQLite cannot drop a CHECK constraint in place, so the table is
rebuilt through `batch_alter_table` with `copy_from` definitions that
mirror the 0005 table exactly (columns, PK, UNIQUE, FKs, and both
indexes). Dropping and re-creating a same-named CHECK must happen in
two separate batch blocks — one batch doing both trips over the
constraint-name lookup.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_reaction_palette"
down_revision: str | Sequence[str] | None = "0005_posts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_PALETTE = (
    "\U0001f44d",
    "\U0001f44e",
    "\u2764\ufe0f",
    "\U0001f680",
    "\U0001f389",
    "\U0001f62e",
    "\U0001f615",
    "\U0001f440",
)
_OLD_CSV = ",".join(f"'{glyph}'" for glyph in _OLD_PALETTE)

_NEW_PALETTE = (
    *_OLD_PALETTE,
    "\U0001faea",
    "\U0001f913",
    "\U0001f9d0",
    "\U0001f60e",
    "\u203c\ufe0f",
    "\u2049\ufe0f",
    "\u2753",
    "\u2757",
    "\u2705",
    "\u274c",
    "\U0001f44f",
    "\U0001f344",
    "\U0001f41e",
    "\u2b50",
    "\U0001f31f",
    "\U0001f525",
    "\U0001f4a7",
    "\U0001f31d",
)
_NEW_CSV = ",".join(f"'{glyph}'" for glyph in _NEW_PALETTE)


def _post_reactions(check_csv: str | None) -> sa.Table:
    constraints: list[sa.Constraint] = [
        sa.PrimaryKeyConstraint("id", name="pk_post_reactions"),
        sa.UniqueConstraint(
            "post_id",
            "user_id",
            "emoji",
            name="uq_post_reactions_post_id_user_id_emoji",
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
    ]
    if check_csv is not None:
        constraints.append(
            sa.CheckConstraint(
                f"emoji IN ({check_csv})",
                name="ck_post_reactions_emoji",
            )
        )
    return sa.Table(
        "post_reactions",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("emoji", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *constraints,
        sa.Index("ix_post_reactions_post_id", "post_id"),
        sa.Index("ix_post_reactions_post_id_emoji", "post_id", "emoji"),
    )


def _swap_check(from_csv: str, to_csv: str) -> None:
    # Constraint names passed to batch ops are expanded through the
    # project naming convention (ck_%(table_name)s_%(constraint_name)s),
    # so the short name "emoji" becomes ck_post_reactions_emoji — the
    # name migration 0005 stored. Passing the full name would double
    # the prefix and the lookup would miss.
    with op.batch_alter_table("post_reactions", copy_from=_post_reactions(from_csv)) as batch_op:
        batch_op.drop_constraint("emoji", type_="check")
    with op.batch_alter_table("post_reactions", copy_from=_post_reactions(None)) as batch_op:
        batch_op.create_check_constraint("emoji", f"emoji IN ({to_csv})")


def upgrade() -> None:
    _swap_check(_OLD_CSV, _NEW_CSV)


def downgrade() -> None:
    _swap_check(_NEW_CSV, _OLD_CSV)
