"""Tests for `outo_models.auth.tokens`.

`TokenService` issues and verifies Personal Access Tokens using PASETO v4
local tokens. The DB never sees the raw token — only its argon2id fingerprint,
and a hypothesis property test asserts that any tampered or substituted token
fails verification.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pyseto
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from outo_models.auth.tokens import (
    DEFAULT_TOKEN_TTL_SECONDS,
    TokenClaims,
    TokenService,
    fingerprint,
    match_fingerprint,
)
from outo_models.exceptions import UnauthorizedError

SECRET = "a-server-side-secret-for-token-signing-only"


@pytest.fixture
def key() -> bytes:
    return hashlib.sha256(SECRET.encode()).digest()


@pytest.fixture
def service(key: bytes) -> TokenService:
    return TokenService(key)


class TestTokenServiceDerivation:
    """`from_settings` derives a 32-byte key from `secret_key` via SHA-256."""

    def test_from_secret_produces_a_working_service(self) -> None:
        ts = TokenService.from_secret(SECRET)
        token = ts.issue("alice", ["read"], ttl_seconds=60)
        claims = ts.verify(token)
        assert claims.subject == "alice"

    def test_from_secret_is_deterministic(self) -> None:
        # Same secret → same key → tokens from one verify in the other.
        a = TokenService.from_secret(SECRET)
        b = TokenService.from_secret(SECRET)
        token = a.issue("alice", ["read"], ttl_seconds=60)
        assert b.verify(token).subject == "alice"

    def test_different_secrets_produce_isolated_tokens(self) -> None:
        a = TokenService.from_secret(SECRET)
        b = TokenService.from_secret("a-completely-different-secret-string-here")
        token = a.issue("alice", ["read"], ttl_seconds=60)
        with pytest.raises(UnauthorizedError):
            b.verify(token)

    def test_from_secret_rejects_empty_secret(self) -> None:
        # Empty secrets are useless (sha256 of empty string is a well-known
        # constant); reject explicitly so we never accidentally sign with one.
        with pytest.raises(ValueError):
            TokenService.from_secret("")


class TestTokenServiceIssue:
    """`issue` mints a PASETO v4 local token with the requested claims."""

    def test_returns_a_string(self, service: TokenService) -> None:
        token = service.issue("alice", ["read"], ttl_seconds=60)
        assert isinstance(token, str)

    def test_token_uses_paseto_v4_local_format(self, service: TokenService) -> None:
        token = service.issue("alice", ["read"], ttl_seconds=60)
        # v4.local.<base64-payload>.<base64-footer>
        assert token.startswith("v4.local.")

    def test_default_ttl_is_90_days(self, service: TokenService) -> None:
        # `exp` is a Unix integer, so bounds must be integer seconds too —
        # `datetime.now()` carries microseconds and would flake the bound.
        before_ts = int(datetime.now(tz=UTC).timestamp())
        token = service.issue("alice", ["read"])
        after_ts = int(datetime.now(tz=UTC).timestamp())
        claims = service.verify(token)
        lo = before_ts + DEFAULT_TOKEN_TTL_SECONDS
        hi = after_ts + DEFAULT_TOKEN_TTL_SECONDS
        assert lo <= int(claims.expires_at.timestamp()) <= hi

    def test_two_issues_for_same_subject_produce_different_tokens(
        self, service: TokenService
    ) -> None:
        # PASETO local tokens are encrypted with a fresh nonce per call.
        a = service.issue("alice", ["read"], ttl_seconds=60)
        b = service.issue("alice", ["read"], ttl_seconds=60)
        assert a != b

    def test_custom_ttl_is_respected(self, service: TokenService) -> None:
        before_ts = int(datetime.now(tz=UTC).timestamp())
        token = service.issue("alice", ["read"], ttl_seconds=120)
        claims = service.verify(token)
        assert before_ts + 119 <= int(claims.expires_at.timestamp()) <= before_ts + 121


class TestTokenServiceVerify:
    """`verify` returns a `TokenClaims` and raises on tamper / expiry."""

    def test_verify_returns_correct_subject(self, service: TokenService) -> None:
        token = service.issue("alice", ["read", "write"], ttl_seconds=60)
        claims = service.verify(token)
        assert claims.subject == "alice"

    def test_verify_returns_correct_scopes(self, service: TokenService) -> None:
        token = service.issue("alice", ["read", "write", "admin"], ttl_seconds=60)
        claims = service.verify(token)
        assert set(claims.scopes) == {"read", "write", "admin"}

    def test_verify_returns_scopes_as_list(self, service: TokenService) -> None:
        # Downstream code may iterate; preserve the original order.
        token = service.issue("alice", ["admin", "read"], ttl_seconds=60)
        claims = service.verify(token)
        assert claims.scopes == ["admin", "read"]

    def test_verify_returns_timezone_aware_expires_at(self, service: TokenService) -> None:
        token = service.issue("alice", ["read"], ttl_seconds=60)
        claims = service.verify(token)
        assert claims.expires_at.tzinfo is not None
        assert claims.expires_at.utcoffset() == timedelta(0)

    def test_tampered_token_raises_unauthorized(self, service: TokenService) -> None:
        token = service.issue("alice", ["read"], ttl_seconds=60)
        head, dot, tail = token.partition(".")
        tampered = f"{head[:-1]}X{dot}{tail}"
        with pytest.raises(UnauthorizedError):
            service.verify(tampered)

    def test_truncated_token_raises_unauthorized(self, service: TokenService) -> None:
        token = service.issue("alice", ["read"], ttl_seconds=60)
        with pytest.raises(UnauthorizedError):
            service.verify(token[: len(token) // 2])

    def test_empty_token_raises_unauthorized(self, service: TokenService) -> None:
        with pytest.raises(UnauthorizedError):
            service.verify("")

    def test_garbage_token_raises_unauthorized(self, service: TokenService) -> None:
        with pytest.raises(UnauthorizedError):
            service.verify("not-a-paseto-token")

    def test_token_signed_by_different_key_raises_unauthorized(self, key: bytes) -> None:
        a = TokenService(key)
        b = TokenService(bytes(b ^ 0xFF for b in key))
        token = a.issue("alice", ["read"], ttl_seconds=60)
        with pytest.raises(UnauthorizedError):
            b.verify(token)

    def test_expired_token_raises_unauthorized(self) -> None:
        # ttl=0 means "expired before the verify call observes the clock".
        ts = TokenService.from_secret(SECRET)
        token = ts.issue("alice", ["read"], ttl_seconds=0)
        with pytest.raises(UnauthorizedError):
            ts.verify(token)

    def test_manually_minted_payload_raises_unauthorized(self, key: bytes) -> None:
        # A token signed with the right key but containing non-JSON garbage
        # in the payload must be rejected — the contract is a JSON object.
        bad_key = pyseto.Key.new(version=4, purpose="local", key=key)
        malformed = pyseto.encode(bad_key, payload="not-a-json-object")
        ts = TokenService(key)
        with pytest.raises(UnauthorizedError):
            ts.verify(malformed.decode())


class TestTokenServiceArbitraryTokens:
    """Property test: any tampered, wrong-key, or garbage token raises."""

    @given(garbage=st.text(min_size=1, max_size=64).filter(lambda s: not s.startswith("v4.")))
    @hyp_settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_arbitrary_garbage_token_raises_unauthorized(
        self, service: TokenService, garbage: str
    ) -> None:
        with pytest.raises(UnauthorizedError):
            service.verify(garbage)


class TestFingerprintHelpers:
    """`fingerprint` and `match_fingerprint` bridge between PASETO and argon2."""

    def test_fingerprint_returns_argon2id_string(self) -> None:
        h = fingerprint("v4.local.sometokenvalue")
        assert h.startswith("$argon2id$")

    def test_fingerprint_is_deterministic_per_call_but_unique(self) -> None:
        # Two calls with the same token must differ (fresh salt each call),
        # but both must round-trip via `match_fingerprint`.
        token = "v4.local.sometokenvalue"
        a = fingerprint(token)
        b = fingerprint(token)
        assert a != b
        assert match_fingerprint(a, token)
        assert match_fingerprint(b, token)

    def test_match_fingerprint_rejects_wrong_token(self) -> None:
        h = fingerprint("v4.local.real-token")
        assert match_fingerprint(h, "v4.local.fake-token") is False

    def test_match_fingerprint_rejects_malformed_hash(self) -> None:
        assert match_fingerprint("not-an-argon2-hash", "any-token") is False


class TestTokenClaimsShape:
    """`TokenClaims` is a frozen, slotted dataclass carrying only the contract fields."""

    def test_token_claims_is_frozen(self) -> None:
        claims = TokenClaims(subject="alice", scopes=["read"], expires_at=datetime.now(tz=UTC))
        with pytest.raises((AttributeError, TypeError)):
            claims.subject = "bob"  # type: ignore[misc]

    def test_token_claims_fields(self) -> None:
        expires_at = datetime(2030, 1, 1, tzinfo=UTC)
        claims = TokenClaims(subject="alice", scopes=["read"], expires_at=expires_at)
        assert claims.subject == "alice"
        assert claims.scopes == ["read"]
        assert claims.expires_at == expires_at
