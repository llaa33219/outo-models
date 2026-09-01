"""Personal Access Token (PAT) ORM model.

The plaintext PAT is never persisted: only its argon2id `fingerprint_hash`
(which the API matches against an incoming token) and a short `prefix`
for display in the web admin UI. `scopes` is a JSON-encoded string list so the
column is portable across sqlite / postgres without a dedicated JSON type.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin
from outo_models.utils.time import utcnow


class PersonalAccessToken(IntIdMixin, TimestampMixin, Base):
    """An opaque, long-lived authentication token issued to a user.

    `fingerprint_hash` is the result of `outo_models.utils.hashing.hash_secret`
    applied to the raw PASETO v4 local token. It is unique per row so a token
    cannot be re-registered. `prefix` carries the first 8 characters of the
    raw token so admins can identify tokens in the UI without revealing them.
    """

    __tablename__ = "personal_access_tokens"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_personal_access_tokens_user_id_users"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_hash: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    scopes: Mapped[str] = mapped_column(String(2000), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_expired(self) -> bool:
        """True iff `expires_at` is set and is in the past.

        Tokens without an expiry are treated as non-expiring for the purposes
        of this property; operators are expected to revoke them explicitly.
        SQLite stores datetimes without tzinfo, so on that dialect we
        re-attach UTC before comparing to `utcnow()`.
        """
        if self.expires_at is None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= utcnow()


__all__ = ["PersonalAccessToken"]
