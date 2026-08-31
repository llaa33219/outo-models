"""Clock helpers. Always return timezone-aware UTC datetimes."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware `datetime` pinned to UTC."""
    return datetime.now(tz=UTC)
