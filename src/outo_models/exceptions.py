"""Typed exception hierarchy for outo-models.

Every error raised inside the application should be (or inherit from) `OutoError`.
HTTP handlers map `.status_code` onto the response; clients / CLIs map `.code`
onto a stable machine-readable identifier that does not change with i18n.
"""

from __future__ import annotations


class OutoError(Exception):
    """Base class for every outo-models error.

    Subclasses set `code` (stable string identifier) and `status_code`
    (HTTP status, used by the FastAPI exception handlers). Both may be
    overridden at construction time if a more specific error needs to be
    surfaced from shared code paths.
    """

    code: str = "outo_error"
    status_code: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ConfigError(OutoError):
    """Configuration is missing or invalid for the current environment."""

    code = "config_error"
    status_code = 500


class NotFoundError(OutoError):
    """Resource does not exist."""

    code = "not_found"
    status_code = 404


class UnauthorizedError(OutoError):
    """Caller is not authenticated."""

    code = "unauthorized"
    status_code = 401


class ForbiddenError(OutoError):
    """Caller is authenticated but lacks permission."""

    code = "forbidden"
    status_code = 403


class ApprovalRequiredError(ForbiddenError):
    """Account exists but admin approval is still pending."""

    code = "approval_required"
    status_code = 403


class RateLimitedError(OutoError):
    """Caller has exceeded the configured request rate."""

    code = "rate_limited"
    status_code = 429


class QuotaExceededError(OutoError):
    """Caller has exceeded their storage / bandwidth quota."""

    code = "quota_exceeded"
    status_code = 413


class ConflictError(OutoError):
    """Request collides with the current resource state."""

    code = "conflict"
    status_code = 409


class ValidationFailedError(OutoError):
    """Caller-supplied input failed semantic validation."""

    code = "validation_failed"
    status_code = 422
