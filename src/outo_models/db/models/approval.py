"""Account-approval ORM model.

Each user gets at most one `Approval` row recording the operator's decision.
`decision` mirrors the user `status` lifecycle so the admin queue can list
`pending` rows without scanning the users table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin


class Approval(IntIdMixin, TimestampMixin, Base):
    """A single, per-user account-approval decision.

    `user_id` is unique because each user has at most one row in this table;
    `decided_by_id` records which admin clicked approve/deny. A row in the
    `pending` state represents an unanswered signup request.
    """

    __tablename__ = "approvals"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_approvals_user_id_users"),
        unique=True,
        nullable=False,
    )
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_approvals_decided_by_id_users"),
        nullable=True,
    )
    decision: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["Approval"]