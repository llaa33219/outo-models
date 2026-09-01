"""Exception handlers for the FastAPI app.

Every typed `OutoError` is mapped to its declared `status_code` with the
shared JSON envelope `{"error": <code>, "message": <str(exc)>}`. Validation
errors from pydantic come in as `RequestValidationError` and are translated
to a 422 with a stable shape so clients can build off `error` instead of
parsing prose. Anything else that escapes a route is caught by the generic
`Exception` handler, logged in full via structlog, and surfaced to the
client as a generic 500 — never the original stack trace, never the
internal class name.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from outo_models.exceptions import OutoError
from outo_models.logging import get_logger

_LOGGER = get_logger(__name__)


def _envelope(code: str, message: str) -> dict[str, str]:
    """Return the standard JSON error envelope: `{"error", "message"}`."""
    return {"error": code, "message": message}


async def _outo_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render any typed `OutoError` as its declared status + JSON envelope."""
    del request  # Unused; the handler signature accepts it for FastAPI.
    assert isinstance(exc, OutoError)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, str(exc)),
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate pydantic `RequestValidationError` into a 422 envelope.

    FastAPI's default 422 shape leaks the raw pydantic error list, which
    is fine for debugging but a fingerprinting risk in production. We keep
    the count of offending fields so clients can decide whether to retry,
    and surface the first message verbatim so the developer still has a
    starting point.
    """
    del request  # Unused; kept for FastAPI's handler signature.
    assert isinstance(exc, RequestValidationError)
    errors: Any = exc.errors()
    first_message: str = "Request validation failed"
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            msg = first.get("msg")
            if isinstance(msg, str) and msg:
                first_message = msg
    return JSONResponse(
        status_code=422,
        content={
            **_envelope("validation_failed", first_message),
            "details": errors if isinstance(errors, list) else [],
        },
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render anything else as a generic 500 — never leak internals.

    The full traceback is logged via structlog with the request path +
    method as context so on-call can correlate the user-facing 500 with the
    server-side exception. The client gets a fixed-shape response whose
    only purpose is to acknowledge the server saw the request and to keep
    the wire format stable.
    """
    _LOGGER.exception(
        "unhandled_error",
        path=request.url.path,
        method=request.method,
    )
    return JSONResponse(
        status_code=500,
        content=_envelope("internal_error", "Internal server error"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler in this module onto `app`.

    Order matters only for the generic `Exception` fallback — FastAPI
    dispatches on the most specific registered exception class, so the
    `OutoError` handler catches every typed subclass before the generic
    one ever sees it.
    """
    app.add_exception_handler(OutoError, _outo_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)


__all__ = ["register_exception_handlers"]
