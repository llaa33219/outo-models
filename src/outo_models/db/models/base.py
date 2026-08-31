"""SQLAlchemy declarative `Base` and shared mixins.

Every model in this package inherits from `Base`, whose `MetaData` carries a
naming convention so Alembic autogen and manual `op.create_*` migrations produce
deterministic constraint identifiers (`pk_users`, `uq_repos_owner_id_kind_name`,
`fk_revisions_repo_id_revisions`, …). Mixins are intentionally tiny — they add
columns but no behavior; behavior lives in repository / service layers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from outo_models.utils.time import utcnow

# Naming convention required by Alembic to auto-name constraints and indexes
# during autogeneration. Names follow the form `<type>_<table>_<col>` so that
# the migration script and the live database agree on identifier strings.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Single shared declarative base for every ORM model.

    Exposes `metadata` with the project naming convention so Alembic migrations
    can reference constraints by name without surprises.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IntIdMixin:
    """Adds an autoincrement integer primary key column named `id`."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class TimestampMixin:
    """Adds a timezone-aware `created_at` column with the current UTC time as default."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class TimestampWithUpdateMixin(TimestampMixin):
    """Adds `created_at` and `updated_at` (both default to UTC now, updated on write)."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "IntIdMixin",
    "TimestampMixin",
    "TimestampWithUpdateMixin",
]