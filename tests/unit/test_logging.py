"""Tests for `outo_models.logging`."""

from __future__ import annotations

import pytest
import structlog

from outo_models.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Restore structlog's default config after each test that mutates it."""
    yield
    structlog.reset_defaults()


class TestConfigureLogging:
    """`configure_logging` selects JSON or console renderer based on env."""

    def test_production_uses_json_renderer(self) -> None:
        configure_logging("production")
        processors = structlog.get_config()["processors"]
        assert any(isinstance(p, structlog.processors.JSONRenderer) for p in processors)

    def test_development_uses_console_renderer(self) -> None:
        configure_logging("development")
        processors = structlog.get_config()["processors"]
        assert any(isinstance(p, structlog.dev.ConsoleRenderer) for p in processors)

    def test_production_renderer_is_last(self) -> None:
        # Renderers must be terminal; placing them last avoids wasted work.
        configure_logging("production")
        processors = structlog.get_config()["processors"]
        assert isinstance(processors[-1], structlog.processors.JSONRenderer)

    def test_development_renderer_is_last(self) -> None:
        configure_logging("development")
        processors = structlog.get_config()["processors"]
        assert isinstance(processors[-1], structlog.dev.ConsoleRenderer)

    def test_idempotent_calls_do_not_raise(self) -> None:
        configure_logging("development")
        configure_logging("production")
        configure_logging("development")


class TestGetLogger:
    """`get_logger` returns a usable stdlib BoundLogger."""

    def test_returns_structlog_stdlib_bound_logger(self) -> None:
        configure_logging("development")
        logger = get_logger("outo_models.test")
        # isinstance check is the public contract; BoundLogger is the return type.
        assert isinstance(logger, structlog.stdlib.BoundLogger)

    def test_named_logger_is_bound(self) -> None:
        configure_logging("development")
        logger = get_logger("outo_models.test")
        # Bound loggers expose the standard logging methods.
        assert callable(logger.info)
        assert callable(logger.warning)
        assert callable(logger.error)

    def test_logger_callable_does_not_raise(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("development")
        logger = get_logger("outo_models.test")
        logger.info("hello", key="value")
        # ConsoleRenderer writes to stdout; we just want to know it didn't blow up.
        # The exact format is unstable across structlog versions, so don't pin it.
