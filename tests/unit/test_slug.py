"""Tests for `outo_models.utils.slug`."""

from __future__ import annotations

import pytest

from outo_models.exceptions import ValidationFailedError
from outo_models.utils.slug import normalize_slug, validate_slug


class TestNormalizeSlug:
    """`normalize_slug` lowercases and strips surrounding whitespace only."""

    def test_lowercases(self) -> None:
        assert normalize_slug("FOO") == "foo"

    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_slug("  foo  ") == "foo"

    def test_preserves_internal_whitespace(self) -> None:
        # normalize does NOT collapse spaces — callers pick a separator if they need one.
        assert normalize_slug("  My Model  ") == "my model"

    def test_empty_input(self) -> None:
        assert normalize_slug("") == ""

    def test_whitespace_only_input(self) -> None:
        assert normalize_slug("   \t\n  ") == ""


class TestValidateSlugAccepts:
    """Inputs that pass the slug policy must round-trip unchanged."""

    @pytest.mark.parametrize(
        "value",
        [
            "a",
            "ab",
            "a-b",
            "a_b",
            "a.b",
            "a-b.c_d",
            "0",
            "123abc",
            "a" * 63,  # boundary: 1 + 62 chars
        ],
    )
    def test_valid_slugs(self, value: str) -> None:
        assert validate_slug(value) == value

    def test_exact_max_length(self) -> None:
        assert len(validate_slug("a" * 63)) == 63


class TestValidateSlugRejects:
    """Inputs that violate the policy must raise ValidationFailedError."""

    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            "-leading-dash",  # starts with '-'
            ".leading-dot",  # starts with '.'
            "trailing-dash-",  # ends with '-'
            "trailing-dot.",  # ends with '.'
            "UPPER",  # uppercase
            "with space",  # space inside
            "with_underscore"[:5] + " ",  # any forbidden char
            "Ω",  # non-ASCII
            "a" * 64,  # too long
            "a" * 100,
            "-",  # single forbidden char
            ".",
            "_leading",  # '_' is allowed mid-string but not at start; first char must be [a-z0-9]
        ],
    )
    def test_invalid_slugs_raise(self, value: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_slug(value)


class TestValidateSlugErrorShape:
    """The raised error must carry the standard `code` / `status_code` fields."""

    def test_status_code_is_422(self) -> None:
        try:
            validate_slug("BAD!")
        except ValidationFailedError as exc:
            assert exc.status_code == 422
            assert isinstance(exc.code, str)
            assert exc.code
        else:
            pytest.fail("expected ValidationFailedError")
