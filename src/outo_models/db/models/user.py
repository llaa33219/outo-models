"""User ORM model.

The user row is the root of nearly every relationship in the system: repos,
revisions, tokens, approvals, and audit entries all reference it. Status drives
whether the user can authenticate (`is_active`); admins are differentiated by
`role` rather than by row flag so admin promotion is a single-column update.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampWithUpdateMixin


class User(IntIdMixin, TimestampWithUpdateMixin, Base):
    """An outo-models account.

    `username` is a slug (enforced at the API boundary, not here); `email`
    is a lowercase canonical form. `password_hash` carries an argon2id blob
    produced by `outo_models.auth.passwords`. `role` is `"user"` or `"admin"`;
    `status` is `"pending"`, `"approved"`, `"denied"`, or `"banned"`.
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(unique=True, nullable=False)
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(nullable=False)

    role: Mapped[str] = mapped_column(
        nullable=False,
        default="user",
        server_default="user",
    )
    status: Mapped[str] = mapped_column(
        nullable=False,
        # The DB-level default is `pending` because that is the safest
        # starting state when `require_approval` is on. Application code is
        # responsible for writing `status="approved"` when the operator has
        # configured `require_approval=False`.
        default="pending",
        server_default="pending",
    )

    display_name: Mapped[str | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", use_alter=True, name="fk_users_approved_by_id_users"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    @property
    def is_active(self) -> bool:
        """True iff the account is currently usable.

        Equivalent to `status == "approved"`. Banned users, denied signups,
        and pending approvals all evaluate False; routes that allow general
        API access should gate on this property, not on `role` or raw status.
        """
        return self.status == "approved"


__all__ = ["User"]
