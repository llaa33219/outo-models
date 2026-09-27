"""Global Posts ORM models (short-form feed + long-form markdown posts).

The Posts feature is a *site-wide* social surface, independent of the
per-repo community tab. Posts are either short (escaped plain text with
preserved line breaks) or long (rendered through the same sanitized
markdown pipeline the model card uses). They carry an optional
`repo_id` so an author can mark "this post is about this model/dataset/space",
and they support per-user emoji reactions from a fixed palette plus
flat comments (no replies in v0.5 — comment threading stays on the repo
community tab).

# allow: SIZE_OK — three small tables that share a foreign-key shape and
# own the Posts contract; splitting them across files would scatter a
# single logical surface.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outo_models.db.models.base import (
    Base,
    IntIdMixin,
    TimestampMixin,
    TimestampWithUpdateMixin,
)
from outo_models.db.models.repo import Repo
from outo_models.db.models.user import User

# The fixed GitHub-style reaction palette. The user requirement locks
# this to exactly these 8 emoji; tests assert on the tuple identity so
# adding a glyph is a deliberate contract change.
REACTION_PALETTE: tuple[str, ...] = (
    "\U0001f44d",  # thumbs up
    "\U0001f44e",  # thumbs down
    "\u2764\ufe0f",  # red heart
    "\U0001f680",  # rocket
    "\U0001f389",  # party / tada
    "\U0001f62e",  # open mouth
    "\U0001f615",  # confused
    "\U0001f440",  # eyes
    "\U0001faea",  # face with bags under eyes
    "\U0001f913",  # nerd face
    "\U0001f9d0",  # face with monocle
    "\U0001f60e",  # smiling face with sunglasses
    "\u203c\ufe0f",  # double exclamation mark
    "\u2049\ufe0f",  # exclamation question mark
    "\u2753",  # red question mark
    "\u2757",  # red exclamation mark
    "\u2705",  # check mark button
    "\u274c",  # cross mark
    "\U0001f44f",  # clapping hands
    "\U0001f344",  # mushroom
    "\U0001f41e",  # lady beetle
    "\u2b50",  # star
    "\U0001f31f",  # glowing star
    "\U0001f525",  # fire
    "\U0001f4a7",  # droplet
    "\U0001f31d",  # full moon face
)
_PALETTE_CSV = ",".join(f"'{glyph}'" for glyph in REACTION_PALETTE)

# Body-length caps (chars). The DB column is `Text`; the API surface
# enforces these caps so the column accepts the cap but not arbitrary blobs.
POST_BODY_SHORT_MAX = 2000
POST_BODY_LONG_MAX = 50000
POST_COMMENT_BODY_MAX = 4000
# Title caps match the model card front-matter label length budget.
POST_TITLE_MAX = 200

POST_KIND_SHORT = "short"
POST_KIND_LONG = "long"
_POST_KIND_VALUES = f"('{POST_KIND_SHORT}', '{POST_KIND_LONG}')"


class Post(IntIdMixin, TimestampWithUpdateMixin, Base):
    """A site-wide post: short text or long-form markdown with an optional repo link.

    `kind='short'` posts render the body as escaped plain text with
    preserved line breaks (no markup). `kind='long'` posts carry a
    required `title` and render `body` through `repos.card`'s
    sanitized-markdown pipeline — the same path the model card uses,
    so the dangerous-tag / `on*` / `javascript:`-URL scrub rules apply
    identically.

    `repo_id` is nullable: posts can be standalone. The Posts feed
    hides the chip when the linked repo is private and the viewer is
    neither the owner nor an admin, so private-repo existence is not
    leaked through a post.

    `author_id` is required; the cascade on the FK is not declared
    because user deletion is not a current code path (the social /
    approval subsystems keep users around as `banned` rather than
    DELETEing the row).
    """

    __tablename__ = "posts"

    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_posts_author_id_users"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    # `title` is NULL for short posts and required for long posts. The
    # CHECK below enforces the asymmetry so a hand-crafted INSERT cannot
    # produce a long-without-title or short-with-title row.
    title: Mapped[str | None] = mapped_column(String(POST_TITLE_MAX), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # `repo_id` is nullable; SET NULL on delete so a removed repo does
    # not cascade-erase the post history. The post still renders, just
    # without the linked-repo chip.
    repo_id: Mapped[int | None] = mapped_column(
        ForeignKey("repos.id", name="fk_posts_repo_id_repos", ondelete="SET NULL"),
        nullable=True,
    )

    author: Mapped[User] = relationship("User", lazy="raise")
    repo: Mapped[Repo | None] = relationship("Repo", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            f"kind IN {_POST_KIND_VALUES}",
            name="ck_posts_kind",
        ),
        # Long posts MUST carry a non-empty title; short posts MUST NOT.
        # The trim check lives in the API layer (`create_post`); the DB
        # guard keeps direct INSERTs honest so a stray tool cannot
        # produce a long-without-title row.
        CheckConstraint(
            "(kind = 'long' AND title IS NOT NULL AND length(title) > 0) OR "
            "(kind = 'short' AND title IS NULL)",
            name="ck_posts_title_kind_consistency",
        ),
        Index("ix_posts_author_id", "author_id"),
        Index("ix_posts_repo_id", "repo_id"),
        Index("ix_posts_created_at", "created_at"),
    )


class PostReaction(IntIdMixin, TimestampMixin, Base):
    """A single user's reaction on a single post.

    `UNIQUE(post_id, user_id, emoji)` makes the toggle idempotent: a
    second POST with the same triple is a no-op (the service layer
    flips removal on instead). The composite index `(post_id, emoji)`
    backs the per-post aggregate count query used by the feed tile
    and the trending-post selector.
    """

    __tablename__ = "post_reactions"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", name="fk_post_reactions_post_id_posts", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_post_reactions_user_id_users"),
        nullable=False,
    )
    emoji: Mapped[str] = mapped_column(String(8), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "post_id",
            "user_id",
            "emoji",
            name="uq_post_reactions_post_id_user_id_emoji",
        ),
        CheckConstraint(
            f"emoji IN ({_PALETTE_CSV})",
            name="ck_post_reactions_emoji",
        ),
        Index("ix_post_reactions_post_id", "post_id"),
        Index("ix_post_reactions_post_id_emoji", "post_id", "emoji"),
    )


class PostComment(IntIdMixin, TimestampMixin, Base):
    """A flat user comment on a post.

    No replies in v0.5 (the Posts feature is intentionally flat); the
    per-repo community tab keeps the parent_id reply chain. The 4000-
    char cap mirrors the per-repo comment cap so the API surface is
    predictable across the two surfaces.

    Cascade on post delete — a `posts` row deletion clears every
    comment row that references it, mirroring the same FK cascade
    used by `post_reactions`.
    """

    __tablename__ = "post_comments"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", name="fk_post_comments_post_id_posts", ondelete="CASCADE"),
        nullable=False,
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_post_comments_author_id_users"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    author: Mapped[User] = relationship("User", lazy="raise")

    __table_args__ = (
        Index("ix_post_comments_post_id", "post_id"),
        Index("ix_post_comments_author_id", "author_id"),
    )


__all__ = [
    "POST_BODY_LONG_MAX",
    "POST_BODY_SHORT_MAX",
    "POST_COMMENT_BODY_MAX",
    "POST_KIND_LONG",
    "POST_KIND_SHORT",
    "POST_TITLE_MAX",
    "REACTION_PALETTE",
    "Post",
    "PostComment",
    "PostReaction",
]
