"""Storage quota ORM models.

`UserQuota` records the operator-set cap; `UserUsage` records the current
byte consumption. They are intentionally separate so the quota can be updated
infrequently while usage is reconciled on every push.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin


class UserQuota(IntIdMixin, TimestampMixin, Base):
    """Per-user storage cap (in bytes).

    `max_bytes` is the cap the operator has assigned. The default is applied
    at the application layer (from `Settings.default_quota_bytes`) so this
    column does not carry a `server_default` — DB rows are only ever created
    by application code that knows the operator's intent.
    """

    __tablename__ = "user_quotas"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_quotas_user_id_users"),
        unique=True,
        nullable=False,
    )
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class UserUsage(IntIdMixin, TimestampMixin, Base):
    """Per-user storage consumption (in bytes).

    `updated_at` is bumped by the quota reconcile job. Read paths answer
    `used_bytes <= user_quota.max_bytes` to gate writes.
    """

    __tablename__ = "user_usages"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_usages_user_id_users"),
        unique=True,
        nullable=False,
    )
    used_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


__all__ = ["UserQuota", "UserUsage"]