"""Web admin settings ORM model.

Generic key/value store for settings the operator can change at runtime via
the web UI (homepage banner, signup-closed toggle, etc.). The auth / config
teams do not consume this — it is the admin UI's surface.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampWithUpdateMixin


class WebSetting(IntIdMixin, TimestampWithUpdateMixin, Base):
    """A single operator-editable web setting, keyed by `key`.

    The contract is intentionally minimal — `value` is a free-form string the
    consumer must parse. Stores never see a structured type so the schema does
    not have to evolve when the UI adds new fields.
    """

    __tablename__ = "web_settings"

    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(2000), nullable=False)


__all__ = ["WebSetting"]
