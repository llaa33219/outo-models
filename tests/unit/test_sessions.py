"""Tests for `outo_models.auth.sessions`.

`SessionManager` is a thin wrapper around `itsdangerous.URLSafeTimedSerializer`
that turns tamper and expiry into the project's standard `UnauthorizedError`.
The cookie helpers expose a single source of truth for the security attributes
applied to every session cookie emitted by FastAPI handlers.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from outo_models.auth.sessions import (
    SESSION_COOKIE_NAME,
    SessionManager,
    cookie_kwargs,
)
from outo_models.exceptions import UnauthorizedError


@pytest.fixture
def manager() -> SessionManager:
    return SessionManager(secret_key="a-test-secret-key-with-enough-entropy", max_age=3600)


class TestSessionManagerRoundTrip:
    """`dumps` / `loads` round-trip a dict losslessly."""

    def test_dumps_returns_a_string(self, manager: SessionManager) -> None:
        token = manager.dumps({"user_id": 42})
        assert isinstance(token, str)

    def test_loads_returns_the_original_dict(self, manager: SessionManager) -> None:
        original = {"user_id": 42, "role": "user"}
        token = manager.dumps(original)
        assert manager.loads(token) == original

    def test_dumps_produces_a_different_token_for_different_data(
        self, manager: SessionManager
    ) -> None:
        # Different inputs must produce different tokens — sanity check
        # that the serializer is not a constant.
        a = manager.dumps({"user_id": 1})
        b = manager.dumps({"user_id": 2})
        assert a != b

    def test_loads_accepts_empty_dict(self, manager: SessionManager) -> None:
        token = manager.dumps({})
        assert manager.loads(token) == {}


class TestSessionManagerTamperAndExpiry:
    """`loads` must reject anything that was not signed by this manager or that is stale."""

    def test_tampered_signature_raises_unauthorized(self, manager: SessionManager) -> None:
        token = manager.dumps({"user_id": 1})
        # Flip a few characters in the signature segment.
        head, sep, sig = token.rpartition(".")
        tampered = f"{head}{sep}{sig[:-2]}AA"
        with pytest.raises(UnauthorizedError):
            manager.loads(tampered)

    def test_truncated_token_raises_unauthorized(self, manager: SessionManager) -> None:
        token = manager.dumps({"user_id": 1})
        with pytest.raises(UnauthorizedError):
            manager.loads(token[: len(token) // 2])

    def test_empty_token_raises_unauthorized(self, manager: SessionManager) -> None:
        with pytest.raises(UnauthorizedError):
            manager.loads("")

    def test_garbage_token_raises_unauthorized(self, manager: SessionManager) -> None:
        with pytest.raises(UnauthorizedError):
            manager.loads("not-a-signed-cookie")

    def test_expired_token_raises_unauthorized(self) -> None:
        # max_age must be strictly negative: itsdangerous uses `age > max_age`,
        # so `max_age=0` is still inside the leeway window and would falsely pass.
        short = SessionManager(secret_key="a-test-secret-key-with-enough-entropy", max_age=-1)
        token = short.dumps({"user_id": 1})
        with pytest.raises(UnauthorizedError):
            short.loads(token)

    def test_token_signed_by_different_secret_is_rejected(self, manager: SessionManager) -> None:
        other = SessionManager(
            secret_key="a-completely-different-secret-key-with-bytes", max_age=3600
        )
        foreign = other.dumps({"user_id": 1})
        with pytest.raises(UnauthorizedError):
            manager.loads(foreign)

    def test_non_dict_payload_raises_unauthorized(self, manager: SessionManager) -> None:
        # A signed but non-dict value must be rejected — the contract is `dict`.
        foreign = SessionManager(
            secret_key="a-test-secret-key-with-enough-entropy", max_age=3600
        ).dumps([1, 2, 3])  # type: ignore[arg-type]
        with pytest.raises(UnauthorizedError):
            manager.loads(foreign)


class TestSessionManagerArbitraryDumps:
    """Property test: any dict we serialize round-trips unchanged."""

    @given(payload=st.dictionaries(st.text(min_size=1, max_size=16), st.integers()))
    @hyp_settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_round_trip_arbitrary_dict(
        self, manager: SessionManager, payload: dict[str, int]
    ) -> None:
        # The function-scoped fixture is intentionally shared across
        # examples: SessionManager holds only the secret and max_age,
        # both immutable, so reusing it is exactly equivalent to rebuilding.
        token = manager.dumps(payload)
        assert manager.loads(token) == payload


class TestCookieConstants:
    """`SESSION_COOKIE_NAME` and `cookie_kwargs` define the wire contract."""

    def test_session_cookie_name_is_stable(self) -> None:
        # Changing this is a breaking change for every browser holding a session.
        assert SESSION_COOKIE_NAME == "outo_session"

    def test_cookie_kwargs_secure_false_omits_secure_flag(self) -> None:
        # Insecure mode (development over HTTP) sets `Secure=False` so the
        # value flows back into the same response shape as production.
        kw = cookie_kwargs(secure=False)
        assert kw["secure"] is False

    def test_cookie_kwargs_secure_true_sets_secure_flag(self) -> None:
        kw = cookie_kwargs(secure=True)
        assert kw["secure"] is True

    @pytest.mark.parametrize("secure", [True, False])
    def test_cookie_kwargs_always_http_only(self, secure: bool) -> None:
        kw = cookie_kwargs(secure=secure)
        assert kw["httponly"] is True

    @pytest.mark.parametrize("secure", [True, False])
    def test_cookie_kwargs_samesite_is_lax(self, secure: bool) -> None:
        kw = cookie_kwargs(secure=secure)
        assert kw["samesite"] == "lax"

    @pytest.mark.parametrize("secure", [True, False])
    def test_cookie_kwargs_path_is_root(self, secure: bool) -> None:
        kw = cookie_kwargs(secure=secure)
        assert kw["path"] == "/"

    def test_cookie_kwargs_has_no_extra_keys(self) -> None:
        # The four keys are the contract; anything else is drift.
        kw = cookie_kwargs(secure=True)
        assert set(kw) == {"secure", "httponly", "samesite", "path"}


class TestCookieKwargsDistinctValues:
    """Property test: secure=True and secure=False actually differ — proves the kwarg is wired."""

    @given(secure=st.booleans())
    @hyp_settings(max_examples=10)
    def test_secure_flag_reflects_input(self, secure: bool) -> None:
        assert cookie_kwargs(secure=secure)["secure"] is secure
