"""Tests for `outo_models.auth.passwords`.

These exercise the auth-team-owned argon2id wrappers and the parameter-upgrade
gate (`needs_rehash`). A hypothesis property test asserts that for *any*
candidate password, only the genuine one verifies.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from outo_models.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)


class TestHashPassword:
    """`hash_password` returns an argon2id-encoded string."""

    def test_returns_argon2id_encoded_string(self) -> None:
        h = hash_password("correct horse battery staple")
        assert h.startswith("$argon2id$")

    def test_each_call_produces_a_different_hash(self) -> None:
        # argon2 uses a fresh random salt per call — same input, different outputs.
        a = hash_password("same-input")
        b = hash_password("same-input")
        assert a != b

    def test_output_is_a_string(self) -> None:
        assert isinstance(hash_password("anything"), str)


class TestVerifyPassword:
    """`verify_password` checks a password against its hash."""

    def test_correct_password_returns_true(self) -> None:
        h = hash_password("the-password")
        assert verify_password(h, "the-password") is True

    def test_wrong_password_returns_false(self) -> None:
        h = hash_password("the-password")
        assert verify_password(h, "not-the-password") is False

    def test_malformed_hash_returns_false(self) -> None:
        # Never raise — verification failures degrade to "no".
        assert verify_password("not-an-argon2-hash", "anything") is False

    def test_empty_hash_returns_false(self) -> None:
        assert verify_password("", "anything") is False

    def test_empty_password_against_real_hash_returns_false(self) -> None:
        h = hash_password("non-empty")
        assert verify_password(h, "") is False

    def test_empty_password_against_empty_hash_returns_false(self) -> None:
        assert verify_password("", "") is False


class TestNeedsRehash:
    """`needs_rehash` flags hashes produced under older / weaker parameters."""

    def test_fresh_hash_does_not_need_rehash(self) -> None:
        # A hash produced by our own hasher already uses the current parameters.
        assert needs_rehash(hash_password("anything")) is False

    def test_weaker_hash_needs_rehash(self) -> None:
        # A hash minted by an argon2 hasher tuned to clearly weaker parameters
        # than our own must be flagged for upgrade on next successful login.
        weak = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
        legacy_hash = weak.hash("legacy-user-password")
        assert needs_rehash(legacy_hash) is True

    def test_malformed_hash_is_treated_as_needing_rehash(self) -> None:
        # An unparseable blob is unusable; the caller should rehash on next login.
        assert needs_rehash("not-an-argon2-hash") is True


class TestArbitraryWrongPasswords:
    """Property test: for any candidate string, the real password is the only one that verifies."""

    @given(
        real=st.text(min_size=1, max_size=64),
        candidate=st.text(min_size=1, max_size=64),
    )
    @hyp_settings(max_examples=50, deadline=None)
    def test_arbitrary_wrong_password_verifies_false(self, real: str, candidate: str) -> None:
        # Given a password and a candidate (typically different from real)
        h = hash_password(real)
        if candidate != real:
            # When verifying the candidate against the hash
            # Then it must never verify
            assert verify_password(h, candidate) is False
        else:
            # Sanity: the real password always verifies.
            assert verify_password(h, candidate) is True


@pytest.mark.parametrize(
    "password",
    ["short", "x" * 128, "with spaces and !@#$%", "🔐unicode🔑"],
)
def test_round_trip_for_pathological_passwords(password: str) -> None:
    """Hashing/verifying must work for the full variety of inputs users will pick."""
    h = hash_password(password)
    assert verify_password(h, password) is True
    assert verify_password(h, password + "x") is False
