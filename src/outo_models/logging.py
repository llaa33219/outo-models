"""Process-wide logging configuration backed by structlog.

`configure_logging()` is called once at startup with the active environment.
It swaps between a colored console renderer (development) and a JSON renderer
(production). After configuration, callers obtain loggers with `get_logger()`.

The implementation pins structlog to its stdlib integration (`structlog.stdlib`)
so loggers satisfy the `structlog.stdlib.BoundLogger` contract the rest of
the codebase documents.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

# Process-wide log level; production deployments override via the entrypoint.
_DEFAULT_LOG_LEVEL = logging.INFO


def configure_logging(env: str) -> None:
    """Configure structlog + stdlib logging for `env`.

    Args:
        env: Either `"production"` (JSON output, machine-readable) or
            anything else (colored console, human-readable).
    """
    # Wire the stdlib root logger so anything that still uses `logging`
    # directly also flows through the same formatter.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=_DEFAULT_LOG_LEVEL,
    )

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if env == "production":
        processors: list[structlog.types.Processor] = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a `structlog.stdlib.BoundLogger` named `name`.

    Materializes the lazy proxy structlog hands out by default so callers
    receive an actual `BoundLogger` instance — the contract the rest of
    the codebase documents and the type checkers can reason about.
    """
    bound = structlog.get_logger(name).bind()
    # `structlog.get_logger` is annotated `-> Any`, so the runtime-true
    # `BoundLogger` needs an explicit cast for the type checker.
    return cast(structlog.stdlib.BoundLogger, bound)
