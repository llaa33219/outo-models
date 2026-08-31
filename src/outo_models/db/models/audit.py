"""Audit-log ORM model.

Append-only audit trail. `actor_id` is nullable because some entries are
emitted by the system (cron jobs, anonymous requests, etc.); `detail` is a
JSON string the caller can serialize however they wish.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin


class AuditLog(IntIdMixin, TimestampMixin, Base):
    """A single audit-log entry.

    `action` is a stable string identifier (e.g. `"user.signup"`,
    `"repo.push"`) and is indexed so the admin UI can filter by it cheaply.
    `target_type` + `target_id` identify the affected resource (a row id
    stringified to be portable across numeric / UUID PKs); `detail` is an
    opaque JSON blob whose schema is owned by the producer of the entry.
    """

    __tablename__ = "audit_logs"

    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_audit_logs_actor_id_users"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_created_at", "created_at"),
    )


__all__ = ["AuditLog"]