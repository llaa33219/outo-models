"""Tests for `outo_models.auth.rate_limit`.

`limiter` is a slowapi `Limiter` constructed without a Flask app; the
named constants are the single source of truth for the rate ceilings each
endpoint applies. Key functions are tested with minimal starlette `Request`
shims — slowapi only ever reads `request.client.host` and (for the user-
aware variant) `request.state.user_id`, so the shim keeps the surface area
exactly that.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from slowapi import Limiter

from outo_models.auth.rate_limit import (
    API_LIMIT,
    GIT_PULL_LIMIT,
    GIT_PUSH_LIMIT,
    LOGIN_LIMIT,
    SIGNUP_LIMIT,
    key_by_ip,
    key_by_user_or_ip,
    limiter,
)


def _fake_request(ip: str = "203.0.113.42") -> SimpleNamespace:
    """Minimal starlette `Request` shim covering what the keyfuncs read."""
    state = SimpleNamespace()
    return SimpleNamespace(client=SimpleNamespace(host=ip), state=state)


class TestLimiterObject:
    """`limiter` is a usable slowapi Limiter with our preferred key function."""

    def test_limiter_is_a_slowapi_limiter(self) -> None:
        assert isinstance(limiter, Limiter)

    def test_limiter_has_a_key_function(self) -> None:
        # A Limiter without a key_func would crash on the first call.
        assert limiter._key_func is not None


class TestKeyByIp:
    """`key_by_ip` returns the remote address from the request."""

    @pytest.mark.parametrize("ip", ["203.0.113.42", "198.51.100.1", "::1", "2001:db8::1"])
    def test_returns_request_client_host(self, ip: str) -> None:
        assert key_by_ip(_fake_request(ip)) == ip

    def test_returns_a_string(self) -> None:
        assert isinstance(key_by_ip(_fake_request()), str)


class TestKeyByUserOrIp:
    """`key_by_user_or_ip` prefers the authenticated user id over the IP."""

    def test_returns_user_id_prefix_when_authenticated(self) -> None:
        request = _fake_request(ip="203.0.113.42")
        request.state.user_id = "alice"
        # Prefix keeps the user buckets distinct from anonymous IP buckets.
        assert key_by_user_or_ip(request) == "user:alice"

    def test_falls_back_to_ip_when_user_id_attribute_missing(self) -> None:
        request = _fake_request(ip="203.0.113.42")
        # `user_id` is not set on `state` at all.
        assert "203.0.113.42" in key_by_user_or_ip(request)

    def test_falls_back_to_ip_when_user_id_is_none(self) -> None:
        request = _fake_request(ip="198.51.100.7")
        request.state.user_id = None
        assert "198.51.100.7" in key_by_user_or_ip(request)

    def test_falls_back_to_ip_when_user_id_is_empty_string(self) -> None:
        request = _fake_request(ip="198.51.100.8")
        request.state.user_id = ""
        assert "198.51.100.8" in key_by_user_or_ip(request)

    def test_returns_string_for_every_input(self) -> None:
        for user_id in ("alice", None, "", 0, 42):
            request = _fake_request(ip="10.0.0.1")
            request.state.user_id = user_id
            assert isinstance(key_by_user_or_ip(request), str)

    def test_user_id_int_does_not_crash(self) -> None:
        # `user_id` is typed loosely upstream; the keyfunc must not raise
        # on whatever shape an old middleware might attach.
        request = _fake_request(ip="10.0.0.1")
        request.state.user_id = 42
        assert key_by_user_or_ip(request) == "user:42"


class TestNamedLimitConstants:
    """Every named limit matches the documented `<count>/<period>` format."""

    @pytest.mark.parametrize(
        "name, value",
        [
            ("LOGIN_LIMIT", LOGIN_LIMIT),
            ("SIGNUP_LIMIT", SIGNUP_LIMIT),
            ("GIT_PUSH_LIMIT", GIT_PUSH_LIMIT),
            ("GIT_PULL_LIMIT", GIT_PULL_LIMIT),
            ("API_LIMIT", API_LIMIT),
        ],
    )
    def test_constant_matches_expected_shape(self, name: str, value: str) -> None:
        assert isinstance(value, str)
        count, _, period = value.partition("/")
        assert count.isdigit(), f"{name} = {value!r} must start with an integer count"
        assert period in {"second", "minute", "hour", "day"}, (
            f"{name} = {value!r} period must be one of second/minute/hour/day"
        )

    def test_login_limit_is_5_per_minute(self) -> None:
        assert LOGIN_LIMIT == "5/minute"

    def test_signup_limit_is_3_per_minute(self) -> None:
        assert SIGNUP_LIMIT == "3/minute"

    def test_git_push_limit_is_30_per_minute(self) -> None:
        assert GIT_PUSH_LIMIT == "30/minute"

    def test_git_pull_limit_is_120_per_minute(self) -> None:
        assert GIT_PULL_LIMIT == "120/minute"

    def test_api_limit_is_240_per_minute(self) -> None:
        assert API_LIMIT == "240/minute"
