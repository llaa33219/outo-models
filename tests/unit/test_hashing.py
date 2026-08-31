"""Tests for `outo_models.utils.hashing`."""

from __future__ import annotations

import pytest

from outo_models.utils.hashing import hash_secret, verify_secret


class TestHashSecret:
    """`hash_secret` returns an argon2-encoded string."""

    def test_returns_argon2id_encoded_string(self) -> None:
        h = hash_secret("correct horse battery staple")
        assert h.startswith("$argon2id$")

    def test_each_call_produces_a_different_hash(self) -> None:
        # argon2 uses a fresh random salt per call — same input, different outputs.
        a = hash_secret("same-input")
        b = hash_secret("same-input")
        assert a != b

    def test_output_is_a_string(self) -> None:
        assert isinstance(hash_secret("anything"), str)


class TestVerifySecret:
    """`verify_secret` checks a secret against its argon2 hash."""

    def test_correct_secret_returns_true(self) -> None:
        h = hash_secret("the-secret")
        assert verify_secret(h, "the-secret") is True

    def test_wrong_secret_returns_false(self) -> None:
        h = hash_secret("the-secret")
        assert verify_secret(h, "not-the-secret") is False

    def test_malformed_hash_returns_false(self) -> None:
        # Never raise — verification failures degrade to "no".
        assert verify_secret("not-an-argon2-hash", "anything") is False

    def test_empty_hash_returns_false(self) -> None:
        assert verify_secret("", "anything") is False

    def test_empty_secret_against_real_hash_returns_false(self) -> None:
        h = hash_secret("non-empty")
        assert verify_secret(h, "") is False

    @pytest.mark.parametrize("attempts", range(5))
    def test_round_trip_repeatedly(self, attempts: int) -> None:
        h = hash_secret(f"secret-{attempts}")
        assert verify_secret(h, f"secret-{attempts}") is True
