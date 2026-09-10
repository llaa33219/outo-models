"""Tests for the HTTP-status → typed-error mapping."""

from __future__ import annotations

import httpx

from outo_models_cli.errors import (
    AuthInvalidError,
    BadResponseError,
    FileTooLargeError,
    ForbiddenError,
    NotFoundError,
    ServerUnreachableError,
    ValidationFailedError,
    map_response_error,
    map_transport_error,
)


def _resp(status: int, body: dict | None = None) -> httpx.Response:
    """Build a synthetic `httpx.Response` for status-mapping tests."""
    if body is None:
        return httpx.Response(status, content=b"")
    return httpx.Response(status, json=body, request=httpx.Request("GET", "http://x"))


def test_map_401_to_auth_invalid() -> None:
    exc = map_response_error(_resp(401))
    assert isinstance(exc, AuthInvalidError)
    assert "auth login" in str(exc)


def test_map_403_to_forbidden() -> None:
    exc = map_response_error(_resp(403))
    assert isinstance(exc, ForbiddenError)


def test_map_404_to_not_found() -> None:
    exc = map_response_error(_resp(404))
    assert isinstance(exc, NotFoundError)


def test_map_413_to_file_too_large() -> None:
    exc = map_response_error(_resp(413))
    assert isinstance(exc, FileTooLargeError)
    assert "git" in str(exc).lower() or "lfs" in str(exc).lower()


def test_map_422_to_validation_failed() -> None:
    exc = map_response_error(_resp(422))
    assert isinstance(exc, ValidationFailedError)


def test_map_500_falls_through_to_bad_response() -> None:
    exc = map_response_error(_resp(500))
    assert isinstance(exc, BadResponseError)


def test_map_429_also_falls_through() -> None:
    """Unmapped status codes surface as BadResponseError — no silent drops."""
    exc = map_response_error(_resp(429))
    assert isinstance(exc, BadResponseError)


def test_detail_in_response_body_is_propagated() -> None:
    exc = map_response_error(_resp(403, body={"detail": "owner required"}))
    assert "owner required" in str(exc)


def test_message_in_response_body_is_propagated() -> None:
    exc = map_response_error(_resp(401, body={"message": "expired token"}))
    assert "expired token" in str(exc)


def test_invalid_json_body_falls_back_to_default() -> None:
    """A non-JSON error body must NOT raise a ValueError — fall through to the default."""
    exc = map_response_error(_resp(403))
    assert isinstance(exc, ForbiddenError)


def test_map_transport_wraps_as_unreachable() -> None:
    """Every `httpx.HTTPError` → `ServerUnreachableError`."""
    exc = map_transport_error(httpx.ConnectError("boom"))
    assert isinstance(exc, ServerUnreachableError)
    assert "Cannot reach" in str(exc)


def test_map_response_includes_status_in_5xx_message() -> None:
    """5xx messages must surface the status code so the operator can diagnose."""
    exc = map_response_error(_resp(503))
    assert "503" in str(exc)


def test_custom_fallback_overrides_default() -> None:
    exc = map_response_error(_resp(404), fallback="repo not found")
    assert "repo not found" in str(exc)


def test_every_omc_error_has_stable_code() -> None:
    """Machine-readable codes must NOT change — grep-able by shell wrappers."""
    assert AuthInvalidError.code == "auth_invalid"
    assert ForbiddenError.code == "forbidden"
    assert NotFoundError.code == "not_found"
    assert FileTooLargeError.code == "file_too_large"
    assert ValidationFailedError.code == "validation_failed"
    assert ServerUnreachableError.code == "server_unreachable"
    assert BadResponseError.code == "bad_response"


class TestTimeoutMessageSplit:
    """A working-but-slow server is NOT 'unreachable' (field failure)."""

    def test_timeout_gets_its_own_message(self):
        import httpx

        from outo_models_cli.errors import map_transport_error

        exc = httpx.ReadTimeout("took too long")
        err = map_transport_error(exc)
        assert "did not respond in time" in str(err)
        assert "Cannot reach" not in str(err)

    def test_connect_error_keeps_unreachable_message(self):
        import httpx

        from outo_models_cli.errors import map_transport_error

        exc = httpx.ConnectError("refused")
        err = map_transport_error(exc)
        assert "Cannot reach the server" in str(err)
