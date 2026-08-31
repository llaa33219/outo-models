"""Tests for `outo_models.utils.time`."""

from __future__ import annotations

from datetime import UTC, datetime

from outo_models.utils.time import utcnow


class TestUtcNow:
    """`utcnow` returns a timezone-aware datetime pinned to UTC."""

    def test_returns_datetime(self) -> None:
        assert isinstance(utcnow(), datetime)

    def test_is_timezone_aware(self) -> None:
        assert utcnow().tzinfo is not None

    def test_is_utc(self) -> None:
        assert utcnow().tzinfo == UTC

    def test_monotonic_within_test(self) -> None:
        first = utcnow()
        second = utcnow()
        assert second >= first
