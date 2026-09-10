"""Typed error hierarchy for `outo-models-cli`.

Every failure surfaced to the user flows through an `OmcError` subclass so
the CLI can render a single clean English line and exit 1, never a Python
traceback (matches the server's `OutoError` discipline in AGENTS.md §2.1).

The `code` is a stable machine identifier the caller (a shell wrapper, a
test) can branch on without parsing prose. The `message` is human-facing
English and may evolve.
"""

from __future__ import annotations

from typing import Any

import httpx


class OmcError(Exception):
    """Base class for every CLI-facing error.

    `code` is the stable identifier (no spaces, no punctuation). `message`
    is the user-facing English line printed by `render_error()`.
    """

    code: str = "omc_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigError(OmcError):
    """Configuration is missing, malformed, or refuses to load."""

    code = "config_error"


class AuthRequiredError(OmcError):
    """No credentials are stored for the requested server."""

    code = "auth_required"


class AuthInvalidError(OmcError):
    """The server rejected the supplied token (HTTP 401)."""

    code = "auth_invalid"


class ForbiddenError(OmcError):
    """The server rejected the request (HTTP 403)."""

    code = "forbidden"


class NotFoundError(OmcError):
    """The server replied 404 for the requested resource."""

    code = "not_found"


class FileTooLargeError(OmcError):
    """Single file exceeds the 100 MiB multipart cap; use git + LFS."""

    code = "file_too_large"


class ValidationFailedError(OmcError):
    """The server rejected the request as malformed (HTTP 422)."""

    code = "validation_failed"


class ServerUnreachableError(OmcError):
    """Cannot reach the server (DNS, connection refused, timeout)."""

    code = "server_unreachable"


class BadResponseError(OmcError):
    """The server replied but the response could not be consumed."""

    code = "bad_response"


class FileMissingError(OmcError):
    """A local file passed to `omc upload` does not exist."""

    code = "file_missing"


# Status code → exception class mapping. Anything outside this table is
# treated as a generic `BadResponseError`.
_STATUS_MAP: dict[int, type[OmcError]] = {
    401: AuthInvalidError,
    403: ForbiddenError,
    404: NotFoundError,
    413: FileTooLargeError,
    422: ValidationFailedError,
}


def map_response_error(response: httpx.Response, *, fallback: str | None = None) -> OmcError:
    """Convert an `httpx.Response` into the appropriate `OmcError`.

    Args:
        response: The error response (status >= 400).
        fallback: Override for the rendered message; defaults to the
            status-mapped text below.

    The function inspects `response.status_code` first and falls back to
    `BadResponseError` for everything else, so a future 418 from the
    server still surfaces as a clean line instead of leaking a traceback.
    """
    status = response.status_code
    detail: str = ""
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(raw, str):
            detail = raw
    message = fallback or _default_message(status, detail)
    cls = _STATUS_MAP.get(status, BadResponseError)
    return cls(message, code=cls.code)


def map_transport_error(exc: httpx.HTTPError) -> OmcError:
    """Wrap an `httpx.HTTPError` as a stable, actionable CLI error.

    Timeouts get their own message (field failure: a slow-but-working
    server was reported as "cannot reach", sending the operator hunting
    for outages). A timeout often means the server is STILL processing —
    retrying a commit can duplicate work, so tell the user to check
    first.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ServerUnreachableError(
            "The server did not respond in time. It may still be processing — "
            "verify with `omc ls` before retrying, then use a longer timeout "
            "if it keeps happening.",
        )
    return ServerUnreachableError(
        "Cannot reach the server. Check the URL, network, and that the server is running.",
    )


def _default_message(status: int, detail: str) -> str:
    if detail:
        return detail
    if status == 401:
        return "Not authenticated. Run `omc auth login --server <url>` to store a token."
    if status == 403:
        return "Forbidden: you do not have permission for this operation."
    if status == 404:
        return "Resource not found."
    if status == 413:
        return (
            "Upload too large. Files over 100 MiB must be uploaded via git + LFS. "
            "Try splitting the file or using `git lfs track`."
        )
    if status == 422:
        return "The server rejected the request as invalid. Check paths and arguments."
    if 400 <= status < 500:
        return f"Request failed (HTTP {status})."
    return f"Server returned an unexpected error (HTTP {status})."


__all__ = [
    "AuthInvalidError",
    "AuthRequiredError",
    "BadResponseError",
    "ConfigError",
    "FileMissingError",
    "FileTooLargeError",
    "ForbiddenError",
    "NotFoundError",
    "OmcError",
    "ServerUnreachableError",
    "ValidationFailedError",
    "map_response_error",
    "map_transport_error",
]
